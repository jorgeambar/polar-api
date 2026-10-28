import os
import hmac
import logging
from typing import Annotated

import pyodbc
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Header, HTTPException

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("polar-api")

app = FastAPI(
    title="Polar API",
    version="0.1.0",
    description="Teste de conectividade com SQL Server"
)


# ==========================================
# AUTENTICAÇÃO
# ==========================================

def require_api_token(
    authorization: Annotated[str | None, Header()] = None
):
    expected = os.getenv("POLAR_API_TOKEN", "")

    if not expected:
        raise HTTPException(
            status_code=503,
            detail="API token not configured"
        )

    scheme, _, token = (authorization or "").partition(" ")

    if (
        scheme.lower() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )


# ==========================================
# CONEXÃO SQL SERVER
# ==========================================

def get_connection():
    required_vars = [
        "SQL_SERVER",
        "SQL_DATABASE",
        "SQL_USERNAME",
        "SQL_PASSWORD"
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
        "ODBC Driver 18 for SQL Server"
    )

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={os.environ['SQL_SERVER']};"
        f"DATABASE={os.environ['SQL_DATABASE']};"
        f"UID={os.environ['SQL_USERNAME']};"
        f"PWD={os.environ['SQL_PASSWORD']};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=8;"
    )

    return pyodbc.connect(
        conn_str,
        timeout=8
    )


# ==========================================
# ROTAS
# ==========================================

@app.get("/")
def root():
    return {
        "service": "Polar API",
        "status": "ok"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ==========================================
# TESTE DE ACESSO AO SQL SERVER
# ==========================================

@app.get(
    "/v1/db-check",
    dependencies=[Depends(require_api_token)]
)
def db_check():

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            result = cursor.fetchone()

        return {
            "api": "ok",
            "database": "connected",
            "query_test": result[0]
        }

    except RuntimeError as e:
        # Erro de configuração, sem expor credenciais
        logger.error("SQL configuration error: %s", e)

        raise HTTPException(
            status_code=503,
            detail=str(e)
        )

    except pyodbc.Error as e:
        # Diagnóstico sanitizado para os logs
        error_text = str(e).lower()

        if (
            "login failed" in error_text
            or "18456" in error_text
        ):
            category = "authentication_failed"

        elif (
            "certificate" in error_text
            or "ssl" in error_text
            or "tls" in error_text
        ):
            category = "tls_error"

        elif (
            "timeout" in error_text
            or "timed out" in error_text
            or "08001" in error_text
            or "10060" in error_text
            or "10061" in error_text
        ):
            category = "network_or_timeout"

        else:
            category = "sql_connection_error"

        # Não registrar a mensagem completa: pode conter
        # informações internas do ambiente.
        logger.warning(
            "SQL connection test failed: category=%s",
            category
        )

        raise HTTPException(
            status_code=503,
            detail={
                "database": "unavailable",
                "category": category
            }
        )

    except Exception:
        logger.exception("Unexpected database test error")

        raise HTTPException(
            status_code=500,
            detail="Unexpected internal error"
        )
