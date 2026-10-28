
import os
import hmac
import logging
import time
import re
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
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)

logger = logging.getLogger("polar-api")

# ==========================================
# FASTAPI
# ==========================================

app = FastAPI(
    title="Polar API",
    version="0.1.1",
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
        logger.error(
            "AUTH | POLAR_API_TOKEN não configurado"
        )
        raise HTTPException(
            status_code=503,
            detail="API token not configured",
        )

    scheme, _, token = (
        (authorization or "").partition(" ")
    )

    if (
        scheme.lower() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        logger.warning("AUTH | Falha de autenticação")

        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

# ==========================================
# CONFIGURAÇÃO SQL
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
        # Registra somente os NOMES das variáveis ausentes
        logger.error(
            "CONFIG | Variáveis ausentes: %s",
            ", ".join(missing),
        )

        raise RuntimeError(
            "Missing configuration: "
            + ", ".join(missing)
        )

    server = os.environ["SQL_SERVER"].strip()
    database = os.environ["SQL_DATABASE"].strip()
    username = os.environ["SQL_USERNAME"]

    driver = os.getenv(
        "SQL_DRIVER",
        "ODBC Driver 18 for SQL Server",
    )

    # Não registrar senha, usuário ou connection string
    logger.info(
        "CONFIG | Configuração presente | "
        "server=%s | database=%s | driver=%s",
        server,
        database,
        driver,
    )

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={os.environ['SQL_PASSWORD']};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=8;"
    )

    start = time.monotonic()

    logger.info(
        "CONNECT | Iniciando conexão com SQL Server"
    )

    try:
        conn = pyodbc.connect(
            conn_str,
            timeout=8,
        )

        elapsed = time.monotonic() - start

        logger.info(
            "CONNECT | SUCESSO | "
            "Conexão estabelecida | elapsed=%.2fs",
            elapsed,
        )

        return conn

    except pyodbc.Error as e:
        elapsed = time.monotonic() - start

        args = e.args
        sqlstate = (
            str(args[0])
            if args and len(str(args[0])) == 5
            else "unknown"
        )

        # Extrai códigos numéricos ODBC quando disponíveis
        native_codes = re.findall(
            r"\((-?\d+)\)",
            str(args[1]) if len(args) > 1 else "",
        )

        # Somente metadados, nunca a mensagem completa
        logger.error(
            "CONNECT | FALHA | "
            "elapsed=%.2fs | sqlstate=%s | "
            "native_codes=%s | error_type=%s",
            elapsed,
            sqlstate,
            native_codes or ["unknown"],
            type(e).__name__,
        )

        # Relança para o tratamento da rota
        raise

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
    start = time.monotonic()

    logger.info("DB_CHECK | Início do teste")

    try:
        # ETAPA 1: conexão
        conn = get_connection()

        # ETAPA 2: consulta
        logger.info(
            "QUERY | Executando SELECT 1"
        )

        query_start = time.monotonic()

        cursor = conn.cursor()
        cursor.timeout = 5
        cursor.execute("SELECT 1 AS ok")
        result = cursor.fetchone()

        query_elapsed = (
            time.monotonic() - query_start
        )

        if result is None:
            logger.error(
                "QUERY | Falha | Nenhuma linha retornada"
            )

            raise HTTPException(
                status_code=503,
                detail={
                    "database": "unavailable",
                    "category": "empty_result",
                },
            )

        logger.info(
            "QUERY | SUCESSO | "
            "elapsed=%.2fs | result=%s",
            query_elapsed,
            result[0],
        )

        logger.info(
            "DB_CHECK | SUCESSO | "
            "total_elapsed=%.2fs",
            time.monotonic() - start,
        )

        return {
            "api": "ok",
            "database": "connected",
            "connection": "established",
            "query_test": result[0],
            "query": "SELECT 1",
        }

    except HTTPException:
        raise

    except RuntimeError:
        # A mensagem detalhada já foi registrada
        # sem valores secretos
        logger.error(
            "DB_CHECK | Abortado | Erro de configuração"
        )

        raise HTTPException(
            status_code=503,
            detail={
                "database": "unavailable",
                "category": "configuration_error",
            },
        )

    except pyodbc.Error as e:
        args = e.args

        sqlstate = (
            str(args[0])
            if args and len(str(args[0])) == 5
            else "unknown"
        )

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
            "hyt00",
            "hyt01",
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

        logger.error(
            "DB_CHECK | FALHA ODBC | "
            "category=%s | sqlstate=%s",
            category,
            sqlstate,
        )

        raise HTTPException(
            status_code=503,
            detail={
                "database": "unavailable",
                "category": category,
                "sqlstate": sqlstate,
            },
        )

    except Exception:
        logger.exception(
            "DB_CHECK | Erro interno inesperado"
        )

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
                logger.info(
                    "CONNECT | Conexão encerrada"
                )
            except Exception:
                logger.warning(
                    "CONNECT | Falha ao fechar conexão"
                )
