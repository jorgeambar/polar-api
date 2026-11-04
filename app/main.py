import os
import hmac
import logging
import time
import re
import socket

from typing import Annotated

import pyodbc

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Header, HTTPException
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
    version="0.2.1",
    description="Diagnóstico de conectividade com SQL Server",
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

def get_sql_config():

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
            "Missing configuration: "
            + ", ".join(missing)
        )

    return {
        "server": os.environ["SQL_SERVER"].strip(),
        "database": os.environ["SQL_DATABASE"].strip(),
        "username": os.environ["SQL_USERNAME"],
        "password": os.environ["SQL_PASSWORD"],
        "driver": os.getenv(
            "SQL_DRIVER",
            "ODBC Driver 18 for SQL Server",
        ),
        "encrypt": os.getenv("SQL_ENCRYPT", "yes"),
        "trust_certificate": os.getenv(
            "SQL_TRUST_SERVER_CERTIFICATE",
            "no",
        ),
    }


# ==========================================
# CONSTRUÇÃO DA CONNECTION STRING
# ==========================================

def odbc_value(value: str) -> str:
    """
    Protege valores que contenham caracteres
    especiais na connection string ODBC.
    """

    return "{" + str(value).replace("}", "}}") + "}"


def build_connection_string(config):

    return (
        f"DRIVER={odbc_value(config['driver'])};"
        f"SERVER={odbc_value(config['server'])};"
        f"DATABASE={odbc_value(config['database'])};"
        f"UID={odbc_value(config['username'])};"
        f"PWD={odbc_value(config['password'])};"
        f"Encrypt={config['encrypt']};"
        f"TrustServerCertificate={config['trust_certificate']};"
        f"Connection Timeout=10;"
    )


# ==========================================
# DIAGNÓSTICO DE ERROS ODBC
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

    # Proteção de informações sensíveis
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
        "error_message": error_message[:1500],
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

    if sqlstate in [
        "HYT00",
        "HYT01",
        "08001",
        "08004",
        "08S01",
    ]:
        return "network_or_timeout"

    if any(term in message for term in [
        "timeout",
        "timed out",
    ]):
        return "network_or_timeout"

    return "sql_connection_error"


# ==========================================
# CONEXÃO SQL SERVER
# ==========================================

def get_connection():

    config = get_sql_config()

    start = time.monotonic()

    logger.info(
        "CONNECT | Iniciando conexão | "
        "server=%s | database=%s | driver=%s | "
        "encrypt=%s | trust_certificate=%s",
        config["server"],
        config["database"],
        config["driver"],
        config["encrypt"],
        config["trust_certificate"],
    )

    try:

        conn = pyodbc.connect(
            build_connection_string(config),
            timeout=10,
        )

        # Timeout de execução das consultas SQL (segundos).
        # Configurado na conexão, não no cursor.
        conn.timeout = 15

        elapsed = time.monotonic() - start

        logger.info(
            "CONNECT | SUCESSO | elapsed=%.2fs",
            elapsed,
        )

        return conn

    except pyodbc.Error as e:

        elapsed = time.monotonic() - start
        details = get_odbc_error_details(e)

        logger.error(
            "CONNECT | FALHA | "
            "elapsed=%.2fs | sqlstate=%s | "
            "native_codes=%s | error_message=%s",
            elapsed,
            details["sqlstate"],
            details["native_codes"],
            details["error_message"],
        )

        raise

    except Exception:

        logger.exception(
            "CONNECT | Erro inesperado ao preparar conexão"
        )

        raise


# ==========================================
# AUXILIAR - FECHAMENTO
# ==========================================

def close_resources(conn=None, cursor=None):

    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            logger.warning("DB | Falha ao fechar cursor")

    if conn is not None:
        try:
            conn.close()
            logger.info("DB | Conexão encerrada")
        except Exception:
            logger.warning("DB | Falha ao fechar conexão")


# ==========================================
# AUXILIAR - ERRO INTERNO
# ==========================================

def internal_error_response(stage, error):

    logger.exception(
        "INTERNAL | Erro inesperado | "
        "stage=%s | error_type=%s",
        stage,
        type(error).__name__,
    )

    raise HTTPException(
        status_code=500,
        detail={
            "stage": stage,
            "status": "failed",
            "category": "internal_error",
            "error_type": type(error).__name__,
        },
    )


# ==========================================
# ROTAS PÚBLICAS
# ==========================================

@app.get("/")
def root():

    return {
        "service": "Polar API",
        "version": "0.2.1",
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

        server = config["server"]

        # Remove prefixo TCP quando existir
        server = re.sub(
            r"(?i)^tcp:",
            "",
            server,
        )

        # Extrai host e porta
        if "," in server:

            host, port = server.rsplit(",", 1)
            port = int(port)

        else:

            host = server
            port = 1433

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
            "TCP | FALHA | stage=%s | error_type=%s",
            stage,
            type(e).__name__,
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
# ETAPA 2 - CONEXÃO REAL SQL SERVER
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

        # ETAPA 1: verificar driver
        stage = "driver_check"

        drivers = pyodbc.drivers()

        config = get_sql_config()

        logger.info(
            "DB_CHECK | Drivers instalados: %s",
            drivers,
        )

        if config["driver"] not in drivers:

            logger.error(
                "DB_CHECK | Driver não encontrado | requested=%s",
                config["driver"],
            )

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

        # ETAPA 2: conexão
        stage = "connection"

        conn = get_connection()

        # ETAPA 3: consulta simples
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

    except RuntimeError:

        logger.exception(
            "DB_CHECK | Falha interna | stage=%s",
            stage,
        )

        raise HTTPException(
            status_code=503,
            detail={
                "stage": stage,
                "status": "failed",
                "category": "configuration_or_query_error",
            },
        )

    except pyodbc.Error as e:

        details = get_odbc_error_details(e)

        category = classify_odbc_error(details)

        logger.error(
            "DB_CHECK | FALHA ODBC | "
            "stage=%s | category=%s | "
            "sqlstate=%s | native_codes=%s | "
            "message=%s",
            stage,
            category,
            details["sqlstate"],
            details["native_codes"],
            details["error_message"],
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
# ETAPA 3 - TESTE REAL DA VIEW
# ==========================================

@app.get(
    "/v1/test",
    dependencies=[Depends(require_api_token)],
)
def test_invoice():

    conn = None
    cursor = None

    stage = "initialization"
    start = time.monotonic()

    logger.info("TEST | Início da consulta real")

    try:

        # ETAPA 1: conexão
        stage = "connection"

        logger.info("TEST | Abrindo conexão")

        conn = get_connection()

        # ETAPA 2: consulta
        stage = "query_execution"

        query = """
            SELECT TOP 1 *
            FROM VIEW_PROTHEUS_GERENCIAL_VENDAS
            WHERE FILIAL = 'POLAR'
            ORDER BY
                DataEmissao DESC,
                NotaFiscal DESC,
                N_Item ASC
        """

        logger.info(
            "TEST | Executando consulta | "
            "view=VIEW_PROTHEUS_GERENCIAL_VENDAS | "
            "filial=POLAR"
        )

        query_start = time.monotonic()

        cursor = conn.cursor()

        cursor.execute(query)

        # ETAPA 3: resultado
        stage = "result_processing"

        result = cursor.fetchone()

        query_elapsed = time.monotonic() - query_start

        if result is None:

            logger.warning(
                "TEST | Consulta executada sem resultados"
            )

            return {
                "stage": "test",
                "status": "success",
                "view": "VIEW_PROTHEUS_GERENCIAL_VENDAS",
                "records_found": 0,
                "message": "Nenhum registro encontrado para FILIAL = POLAR",
            }

        columns = [
            column[0]
            for column in cursor.description
        ]

        first_record = dict(
            zip(columns, result)
        )

        first_record = jsonable_encoder(first_record)

        total_elapsed = time.monotonic() - start

        logger.info(
            "TEST | SUCESSO | "
            "columns=%s | query_elapsed=%.3fs | "
            "total_elapsed=%.3fs",
            len(columns),
            query_elapsed,
            total_elapsed,
        )

        return {
            "stage": "test",
            "status": "success",
            "view": "VIEW_PROTHEUS_GERENCIAL_VENDAS",
            "records_found": 1,
            "query_elapsed_seconds": round(query_elapsed, 3),
            "total_elapsed_seconds": round(total_elapsed, 3),
            "data": first_record,
        }

    except HTTPException:
        raise

    except pyodbc.Error as e:

        details = get_odbc_error_details(e)

        category = classify_odbc_error(details)

        logger.error(
            "TEST | FALHA ODBC | "
            "stage=%s | category=%s | "
            "sqlstate=%s | native_codes=%s | "
            "message=%s",
            stage,
            category,
            details["sqlstate"],
            details["native_codes"],
            details["error_message"],
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

    except Exception as e:

        logger.exception(
            "TEST | Erro inesperado | "
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
