import os
import hmac
import logging
from typing import Annotated

import pyodbc
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Header, HTTPException

load_dotenv()

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("polar-api")

# ==========================================
# FASTAPI
# ==========================================

app = FastAPI(
    title="Polar API",
    version="0.1.0",
    description="Teste de conectividade com SQL Server",
)

# ==========================================
# AUTENTICAÇÃO
# ==========================================

def require_api_token(
    authorization: Annotated[str | None, Header()] = None
):
    expected = os.getenv("POLAR_API_TOKEN", "")

    if not expected:
        logger.error("POLAR_API_TOKEN is not configured")
        raise HTTPException(
            status_code=503,
            detail="API token not configured",
        )

    scheme, _, token = (authorization or "").partition(" ")

    if (
        scheme.lower() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        logger.warning("API authentication failed")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

# ==========================================
# CONEXÃO SQL SERVER
# ==========================================

def get_connection():
    required_vars = [
        "SQL_SERVER",
        "SQL_DATABASE",
        "SQL_USERNAME",
        "SQL_PASSWORD",
    ]

    missing = [
        var for var in required_vars
        if not os.getenv(var)
    ]

    if missing:
        raise RuntimeError(
            "Missing configuration: " + ", ".join(missing)
        )

    driver = os.getenv(
        "SQL_DRIVER",
        "ODBC Driver 18 for SQL Server",
    )

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={os.environ['SQL_SERVER']};"
        f"DATABASE={os.environ['SQL_DATABASE']};"
        f"UID={os.environ['SQL_USERNAME']};"
        f"PWD={os.environ['SQL_PASSWORD']};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=5;"
    )

    return pyodbc.connect(
        conn_str,
        timeout=5,
    )

# ==========================================
# ROTAS PÚBLICAS
# ==========================================

@app.get("/")
def root():
    return {
        "service": "Polar API",
        "status": "ok",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
    }

# ==========================================
# TESTE DE CONEXÃO COM SQL SERVER
# ==========================================

@app.get(
    "/v1/db-check",
    dependencies=[Depends(require_api_token)],
)
def db_check():
    conn = None

    try:
        # Abre a conexão
        conn = get_connection()

        # Timeout da execução da consulta (segundos)
        cursor = conn.cursor()
        cursor.timeout = 3

        # Consulta mínima: não acessa tabelas nem views
        cursor.execute("SELECT 1 AS ok")

        result = cursor.fetchone()

        if result is None:
            logger.error("DB check returned no rows")
            raise HTTPException(
                status_code=503,
                detail={
                    "database": "unavailable",
                    "category": "empty_result",
                },
            )

        logger.info("DB check successful")

        return {
            "api": "ok",
            "database": "connected",
            "query_test": result[0],
        }

    except HTTPException:
        raise

    except RuntimeError as e:
        # Configuração ausente
        logger.error("SQL configuration error: %s", e)

        raise HTTPException(
            status_code=503,
            detail={
                "database": "unavailable",
                "category": "configuration_error",
            },
        )

    except pyodbc.Error as e:
        # Diagnóstico sanitizado
        args = e.args

        sqlstate = (
            str(args[0])
            if args and len(str(args[0])) == 5
            else "unknown"
        )

        native_code = "unknown"

        # Mensagens ODBC geralmente incluem:
        # ('08001', '[08001] ... (10060) ...')
        if len(args) > 1:
            import re

            match = re.search(
                r"\((-?\d+)\)",
                str(args[1]),
            )

            if match:
                native_code = match.group(1)

        error_text = str(e).lower()

        if (
            "login failed" in error_text
            or "18456" in error_text
        ):
            category = "authentication_failed"

        elif any(x in error_text for x in [
            "certificate",
            "ssl",
            "tls",
        ]):
            category = "tls_error"

        elif any(x in error_text for x in [
            "timeout",
            "timed out",
            "08001",
            "08004",
            "08s01",
            "10060",
            "10061",
            "11001",
            "11004",
        ]):
            category = "network_or_timeout"

        else:
            category = "sql_connection_error"

        logger.warning(
            "SQL check failed | category=%s | sqlstate=%s | native_code=%s",
            category,
            sqlstate,
            native_code,
        )

        raise HTTPException(
            status_code=503,
            detail={
                "database": "unavailable",
                "category": category,
                "sqlstate": sqlstate,
                "native_code": native_code,
            },
        )

    except Exception:
        logger.exception("Unexpected DB check error")

        raise HTTPException(
            status_code=500,
            detail={
                "database": "unavailable",
                "category": "internal_error",
            },
        )

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.warning("Failed to close SQL connection")
