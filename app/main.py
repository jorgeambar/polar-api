import os
import hmac
import logging
import time
import re
import socket

from typing import Annotated

import pyodbc

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder


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
    version="0.4.0",
    description="Diagnóstico e consulta SQL Server",
)


# ==========================================
# AUTENTICAÇÃO
# ==========================================

def require_api_token(
    authorization: Annotated[str | None, Header()] = None
):
    expected = os.getenv("POLAR_API_TOKEN", "")

    if not expected:
        logger.error("AUTH | POLAR_API_TOKEN não configurado")
        raise HTTPException(
            status_code=503,
            detail="API token not configured",
        )

    scheme, _, token = (authorization or "").partition(" ")

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

def get_sql_config():

    required_vars = [
        "SQL_SERVER",
        "SQL_PORT",
        "SQL_DATABASE",
        "SQL_USER",
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

    return {
        "server": os.environ["SQL_SERVER"].strip(),
        "port": int(os.environ["SQL_PORT"]),
        "database": os.environ["SQL_DATABASE"].strip(),
        "username": os.environ["SQL_USER"],
        "password": os.environ["SQL_PASSWORD"],
        "driver": os.getenv(
            "SQL_DRIVER",
            "ODBC Driver 18 for SQL Server",
        ),
        "encrypt": os.getenv("SQL_ENCRYPT", "no").lower(),
        "trust_certificate": os.getenv(
            "SQL_TRUST_SERVER_CERTIFICATE",
            "yes",
        ).lower(),
        "connection_timeout": int(
            os.getenv("SQL_CONNECTION_TIMEOUT", "15")
        ),
        "query_timeout": int(
            os.getenv("SQL_QUERY_TIMEOUT", "30")
        ),
    }


# ==========================================
# CONNECTION STRING
# ==========================================

def odbc_value(value: str) -> str:
    """
    Protege valores com caracteres especiais
    na connection string ODBC.
    """

    return "{" + str(value).replace("}", "}}") + "}"


def build_connection_string(config):

    server = (
        f"tcp:{config['server']},{config['port']}"
    )

    return (
        f"DRIVER={odbc_value(config['driver'])};"
        f"SERVER={server};"
        f"DATABASE={odbc_value(config['database'])};"
        f"UID={odbc_value(config['username'])};"
        f"PWD={odbc_value(config['password'])};"
        f"Encrypt={config['encrypt']};"
        f"TrustServerCertificate={config['trust_certificate']};"
        f"Connection Timeout={config['connection_timeout']};"
    )


# ==========================================
# DIAGNÓSTICO ODBC
# ==========================================

def get_odbc_error_details(error):

    args = error.args

    sqlstate = (
        str(args[0])
        if args and len(str(args[0])) == 5
        else "unknown"
    )

    error_message = (
        str(args[1])
        if len(args) > 1
        else str(error)
    )

    # Evita registrar credenciais em mensagens ODBC.
    error_message = re.sub(
        r"(?i)(PWD|PASSWORD)\s*=\s*[^;}\s]+",
        r"\1=***",
        error_message,
    )

    native_codes = re.findall(
        r"\((-?\d+)\)",
        error_message,
    )

    return {
        "sqlstate": sqlstate,
        "native_codes": native_codes or ["unknown"],
        "error_message": error_message[:2000],
    }


def classify_odbc_error(details):

    sqlstate = details["sqlstate"]
    message = details["error_message"].lower()

    if (
        "login failed" in message
        or "18456" in message
    ):
        return "authentication_failed"

    if any(term in message for term in [
        "certificate",
        "ssl",
        "tls",
        "handshake",
    ]):
        return "tls_error"

    if sqlstate in ["HYT00", "HYT01"]:
        return "connection_or_query_timeout"

    if sqlstate in ["08001", "08004", "08S01"]:
        return "network_or_connection_error"

    if any(term in message for term in [
        "timeout",
        "timed out",
    ]):
        return "connection_or_query_timeout"

    return "sql_error"


def log_odbc_error(prefix, stage, error):

    details = get_odbc_error_details(error)
    category = classify_odbc_error(details)

    logger.error(
        "%s | FALHA ODBC | "
        "stage=%s | category=%s | "
        "sqlstate=%s | native_codes=%s | "
        "message=%s",
        prefix,
        stage,
        category,
        details["sqlstate"],
        details["native_codes"],
        details["error_message"],
    )

    return details, category


def raise_odbc_http_error(stage, error, prefix):

    details, category = log_odbc_error(
        prefix,
        stage,
        error,
    )

    raise HTTPException(
        status_code=503,
        detail={
            "stage": stage,
            "status": "failed",
            "category": category,
            "sqlstate": details["sqlstate"],
            "native_codes": details["native_codes"],
        },
    )


# ==========================================
# CONEXÃO SQL SERVER
# ==========================================

def get_connection():

    config = get_sql_config()

    start = time.monotonic()

    logger.info(
        "CONNECT | Iniciando conexão | "
        "server=%s | port=%s | database=%s | "
        "driver=%s | encrypt=%s | "
        "trust_certificate=%s | timeout=%ss",
        config["server"],
        config["port"],
        config["database"],
        config["driver"],
        config["encrypt"],
        config["trust_certificate"],
        config["connection_timeout"],
    )

    try:

        conn = pyodbc.connect(
            build_connection_string(config),
            timeout=config["connection_timeout"],
        )

        # Equivalente ao timeout de execução de consultas.
        # Não configurar timeout no cursor.
        conn.timeout = config["query_timeout"]

        elapsed = time.monotonic() - start

        logger.info(
            "CONNECT | SUCESSO | elapsed=%.3fs",
            elapsed,
        )

        return conn

    except pyodbc.Error as e:

        elapsed = time.monotonic() - start
        details = get_odbc_error_details(e)
        category = classify_odbc_error(details)

        logger.error(
            "CONNECT | FALHA | elapsed=%.3fs | "
            "category=%s | sqlstate=%s | "
            "native_codes=%s | message=%s",
            elapsed,
            category,
            details["sqlstate"],
            details["native_codes"],
            details["error_message"],
        )

        raise

    except Exception:

        logger.exception(
            "CONNECT | ERRO INESPERADO"
        )

        raise


# ==========================================
# FECHAMENTO DE RECURSOS
# ==========================================

def close_resources(conn=None, cursor=None):

    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            logger.exception("DB | Erro ao fechar cursor")

    if conn is not None:
        try:
            conn.close()
            logger.info("DB | Conexão encerrada")
        except Exception:
            logger.exception("DB | Erro ao fechar conexão")


# ==========================================
# ROTAS PÚBLICAS
# ==========================================

@app.get("/")
def root():

    return {
        "service": "Polar API",
        "version": "0.4.0",
        "status": "ok",
    }


@app.get("/health")
def health():

    return {
        "status": "ok",
    }


# ==========================================
# ETAPA 1 - TESTE TCP
# ==========================================

@app.get(
    "/v1/tcp-check",
    dependencies=[Depends(require_api_token)],
)
def tcp_check():

    stage = "configuration"
    start = time.monotonic()

    try:

        config = get_sql_config()

        host = config["server"]
        port = config["port"]

        stage = "tcp_connection"

        logger.info(
            "TCP | Testando conexão | target=%s:%s",
            host,
            port,
        )

        with socket.create_connection(
            (host, port),
            timeout=10,
        ):

            elapsed = time.monotonic() - start

            logger.info(
                "TCP | SUCESSO | elapsed=%.3fs",
                elapsed,
            )

            return {
                "stage": "tcp",
                "status": "success",
                "host": host,
                "port": port,
                "elapsed_seconds": round(elapsed, 3),
            }

    except Exception as e:

        logger.exception(
            "TCP | FALHA | stage=%s | "
            "error_type=%s | message=%s",
            stage,
            type(e).__name__,
            str(e),
        )

        raise HTTPException(
            status_code=503,
            detail={
                "stage": "tcp",
                "status": "failed",
                "failed_at": stage,
                "error_type": type(e).__name__,
            },
        )


# ==========================================
# ETAPA 2 - CONEXÃO SQL + SELECT 1
# ==========================================

@app.get(
    "/v1/db-check",
    dependencies=[Depends(require_api_token)],
)
def db_check():

    conn = None
    cursor = None

    stage = "initialization"
    start = time.monotonic()

    logger.info("DB_CHECK | Início")

    try:

        # 1. Verificar driver
        stage = "driver_check"

        drivers = pyodbc.drivers()
        config = get_sql_config()

        logger.info(
            "DB_CHECK | Drivers instalados: %s",
            drivers,
        )

        if config["driver"] not in drivers:

            raise HTTPException(
                status_code=503,
                detail={
                    "stage": stage,
                    "status": "failed",
                    "category": "driver_not_installed",
                    "requested_driver": config["driver"],
                    "installed_drivers": drivers,
                },
            )

        logger.info(
            "DB_CHECK | Driver encontrado: %s",
            config["driver"],
        )

        # 2. Conectar ao SQL Server
        stage = "connection"

        conn = get_connection()

        # 3. Executar consulta simples
        stage = "query_execution"

        logger.info(
            "DB_CHECK | Executando SELECT 1"
        )

        query_start = time.monotonic()

        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 AS connection_test"
        )

        result = cursor.fetchone()

        query_elapsed = time.monotonic() - query_start

        if result is None:
            raise RuntimeError(
                "SELECT 1 não retornou resultado"
            )

        total_elapsed = time.monotonic() - start

        logger.info(
            "DB_CHECK | SUCESSO | "
            "result=%s | query_elapsed=%.3fs | "
            "total_elapsed=%.3fs",
            result[0],
            query_elapsed,
            total_elapsed,
        )

        return {
            "stage": "db",
            "status": "success",
            "driver": config["driver"],
            "database": "connected",
            "query": "SELECT 1",
            "result": result[0],
            "query_elapsed_seconds": round(query_elapsed, 3),
            "total_elapsed_seconds": round(total_elapsed, 3),
        }

    except HTTPException:
        raise

    except pyodbc.Error as e:

        raise_odbc_http_error(
            stage,
            e,
            "DB_CHECK",
        )

    except Exception as e:

        logger.exception(
            "DB_CHECK | ERRO INESPERADO | "
            "stage=%s | error_type=%s | message=%s",
            stage,
            type(e).__name__,
            str(e),
        )

        raise HTTPException(
            status_code=500,
            detail={
                "stage": stage,
                "status": "failed",
                "category": "internal_error",
                "error_type": type(e).__name__,
            },
        )

    finally:

        close_resources(conn, cursor)


# ==========================================
# ETAPA 3 - CONSULTA REAL NA SB1G10
# ==========================================

@app.get(
    "/v1/test",
    dependencies=[Depends(require_api_token)],
)
def test_sku(
    sku: str = Query(
        ...,
        min_length=1,
        description="Código do produto (B1_COD)",
    )
):

    conn = None
    cursor = None

    stage = "initialization"
    start = time.monotonic()

    sku = sku.strip()

    logger.info(
        "SB1_TEST | Início | sku=%r",
        sku,
    )

    try:

        # 1. Abrir conexão
        stage = "connection"

        conn = get_connection()

        # 2. Preparar consulta
        stage = "query_execution"

        query = """
            SELECT TOP 1 *
            FROM SB1G10
            WHERE
                TRIM(B1_COD) = ?
                OR TRY_CONVERT(
                    BIGINT,
                    TRIM(B1_COD)
                ) = TRY_CONVERT(
                    BIGINT,
                    ?
                )
        """

        logger.info(
            "SB1_TEST | Executando consulta | "
            "table=SB1G10 | sku=%r",
            sku,
        )

        query_start = time.monotonic()

        cursor = conn.cursor()

        # Parâmetros posicionais do pyodbc.
        # Evita concatenar o SKU diretamente no SQL.
        cursor.execute(
            query,
            sku,
            sku,
        )

        # 3. Processar resultado
        stage = "result_processing"

        result = cursor.fetchone()

        query_elapsed = time.monotonic() - query_start

        if result is None:

            total_elapsed = time.monotonic() - start

            logger.warning(
                "SB1_TEST | SKU não encontrado | "
                "sku=%r | query_elapsed=%.3fs",
                sku,
                query_elapsed,
            )

            return {
                "stage": "sb1_test",
                "status": "success",
                "sku": sku,
                "found": False,
                "message": "Nenhum produto encontrado",
                "query_elapsed_seconds": round(
                    query_elapsed, 3
                ),
                "total_elapsed_seconds": round(
                    total_elapsed, 3
                ),
            }

        columns = [
            column[0]
            for column in cursor.description
        ]

        record = dict(
            zip(columns, result)
        )

        record = jsonable_encoder(record)

        total_elapsed = time.monotonic() - start

        logger.info(
            "SB1_TEST | SUCESSO | "
            "sku=%r | columns=%s | "
            "query_elapsed=%.3fs | total_elapsed=%.3fs",
            sku,
            len(columns),
            query_elapsed,
            total_elapsed,
        )

        return {
            "stage": "sb1_test",
            "status": "success",
            "sku": sku,
            "found": True,
            "query_elapsed_seconds": round(
                query_elapsed, 3
            ),
            "total_elapsed_seconds": round(
                total_elapsed, 3
            ),
            "data": record,
        }

    except HTTPException:
        raise

    except pyodbc.Error as e:

        raise_odbc_http_error(
            stage,
            e,
            "SB1_TEST",
        )

    except Exception as e:

        logger.exception(
            "SB1_TEST | ERRO INESPERADO | "
            "stage=%s | sku=%r | "
            "error_type=%s | message=%s",
            stage,
            sku,
            type(e).__name__,
            str(e),
        )

        raise HTTPException(
            status_code=500,
            detail={
                "stage": stage,
                "status": "failed",
                "category": "internal_error",
                "error_type": type(e).__name__,
            },
        )

    finally:

        close_resources(conn, cursor)
