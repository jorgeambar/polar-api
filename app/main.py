
import json
import logging
import os
import secrets
import socket
import time
from datetime import date
from decimal import Decimal

import pyodbc
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


# ============================================================
# CONFIGURAÇÃO
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("polar-api")


def get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)

    if value is None or not value.strip():
        raise RuntimeError(f"Variável de ambiente obrigatória: {name}")

    return value.strip()


SQL_SERVER = get_env("SQL_SERVER")
SQL_PORT = int(os.getenv("SQL_PORT", "1433"))
SQL_DATABASE = get_env("SQL_DATABASE")
SQL_USER = get_env("SQL_USER")
SQL_PASSWORD = get_env("SQL_PASSWORD")

SQL_DRIVER = os.getenv(
    "SQL_DRIVER",
    "ODBC Driver 18 for SQL Server",
)

SQL_ENCRYPT = os.getenv("SQL_ENCRYPT", "no").lower()
SQL_TRUST_SERVER_CERTIFICATE = (
    os.getenv("SQL_TRUST_SERVER_CERTIFICATE", "yes").lower()
)

SQL_CONNECTION_TIMEOUT = int(
    os.getenv("SQL_CONNECTION_TIMEOUT", "15")
)

SQL_QUERY_TIMEOUT = int(
    os.getenv("SQL_QUERY_TIMEOUT", "30")
)

POLAR_API_TOKEN = get_env("POLAR_API_TOKEN")


# ============================================================
# APLICAÇÃO
# ============================================================

app = FastAPI(
    title="Polar API",
    description="API de integração com o banco de dados Protheus.",
    version="1.0.0",
)


# ============================================================
# AUTENTICAÇÃO
# ============================================================

def require_api_token(
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Token de autenticação não informado.",
        )

    scheme, separator, token = authorization.partition(" ")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(token, POLAR_API_TOKEN)
    ):
        raise HTTPException(
            status_code=401,
            detail="Token de autenticação inválido.",
        )


# ============================================================
# CONEXÃO SQL SERVER
# ============================================================

def odbc_value(value: str) -> str:
    """
    Escapa valores para uso na connection string ODBC.
    """
    return "{" + value.replace("}", "}}") + "}"


def get_connection_string() -> str:
    server = f"tcp:{SQL_SERVER},{SQL_PORT}"

    return (
        f"DRIVER={odbc_value(SQL_DRIVER)};"
        f"SERVER={server};"
        f"DATABASE={odbc_value(SQL_DATABASE)};"
        f"UID={odbc_value(SQL_USER)};"
        f"PWD={odbc_value(SQL_PASSWORD)};"
        f"Encrypt={SQL_ENCRYPT};"
        f"TrustServerCertificate={SQL_TRUST_SERVER_CERTIFICATE};"
        f"Connection Timeout={SQL_CONNECTION_TIMEOUT};"
    )


def get_db_connection():
    """
    Abre uma conexão SQL Server.
    Cada requisição recebe sua própria conexão.
    """
    try:
        conn = pyodbc.connect(
            get_connection_string(),
            timeout=SQL_CONNECTION_TIMEOUT,
            autocommit=True,
        )

        conn.timeout = SQL_QUERY_TIMEOUT

        return conn

    except pyodbc.Error:
        logger.exception("Erro ao conectar ao SQL Server.")
        raise


def execute_query(query: str, params: tuple = ()) -> list[dict]:
    """
    Executa uma consulta parametrizada e converte o resultado
    em uma lista de dicionários.
    """
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(query, params)

        columns = [
            column[0]
            for column in cursor.description
        ] if cursor.description else []

        rows = []

        for row in cursor.fetchall():
            rows.append(
                {
                    columns[index]: row[index]
                    for index in range(len(columns))
                }
            )

        return jsonable_encoder(rows)

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None:
            conn.close()


def execute_scalar(query: str, params: tuple = None):
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(query, params or ())

        row = cursor.fetchone()

        return row[0] if row else None

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None:
            conn.close()


# ============================================================
# UTILITÁRIOS
# ============================================================

def normalize_sku(sku: str | None) -> str:
    return (sku or "").strip()


def validate_date(value: str | None, field_name: str):
    if not value:
        return None

    try:
        return date.fromisoformat(value)

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} deve estar no formato YYYY-MM-DD.",
        )


def validate_limit(limit: int | None):
    if limit is None:
        return None

    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=400,
            detail="O limite deve estar entre 1 e 1000.",
        )

    return limit


def normalize_cnpj(cnpj: str) -> str:
    return "".join(
        character
        for character in cnpj
        if character.isdigit()
    )


def find_numeric_sku_match(sku: str, products: list[dict]):
    """
    Procura primeiro o SKU exato.
    Se não encontrar e o SKU for numérico, permite equivalência
    entre códigos com e sem zeros à esquerda.
    """
    normalized = normalize_sku(sku)

    for product in products:
        code = str(product.get("B1_COD") or "").strip()

        if code == normalized:
            return product

    if not normalized.isdigit():
        return None

    numeric_sku = int(normalized)

    for product in products:
        code = str(product.get("B1_COD") or "").strip()

        if code.isdigit() and int(code) == numeric_sku:
            return product

    return None


# ============================================================
# REPOSITÓRIO DE PRODUTOS
# ============================================================

PRODUCT_STRUCTURE_SELECT = """
    SELECT
        PRODUTO.B1_COD,
        PRODUTO.B1_DESC,
        PRODUTO.B1_PESO,

        ESTRUTURA.G1_COMP,
        COMPONENTE.B1_DESC AS B1_DESC_EMBALAGEM,
        ESTRUTURA.G1_QUANT,

        EMBALAGEM.B5_LARG,
        EMBALAGEM.B5_COMPR,
        EMBALAGEM.B5_ESPESS

    FROM SB1G10 AS PRODUTO

    INNER JOIN SG1G10 AS ESTRUTURA
        ON TRIM(ESTRUTURA.G1_COD) =
           TRIM(PRODUTO.B1_COD)

    INNER JOIN SB1G10 AS COMPONENTE
        ON TRIM(COMPONENTE.B1_COD) =
           TRIM(ESTRUTURA.G1_COMP)

    LEFT JOIN SB5G10 AS EMBALAGEM
        ON TRIM(EMBALAGEM.B5_COD) =
           TRIM(ESTRUTURA.G1_COMP)

    WHERE
        AND TRIM(ISNULL(ESTRUTURA.D_E_L_E_T_, '')) <> '*'
        AND TRIM(COMPONENTE.B1_TIPO) = 'EM'

        AND TRIM(COMPONENTE.B1_COD) NOT IN (
            '000054',
            '000055',
            '000057',
            '000163',
            '000384',
            '000389',
            '000735',
            '000927',
            '000995',
            '001499',
            '001571',
            '001640',
            '002374',
            '002710',
            '002711',
            '002712',
            '002756',
            '002757',
            '002758',
            '002813',
            '000205'
        )
"""


def find_product_structures(skus: list[str]) -> list[dict]:
    normalized_skus = list(
        dict.fromkeys(
            normalize_sku(sku)
            for sku in skus
            if normalize_sku(sku)
        )
    )

    if not normalized_skus:
        return []

    conditions = []
    params = []

    for sku in normalized_skus:
        conditions.append(
            """
            (
                TRIM(PRODUTO.B1_COD) = ?
                OR
                TRY_CONVERT(BIGINT, TRIM(PRODUTO.B1_COD)) =
                TRY_CONVERT(BIGINT, ?)
            )
            """
        )

        params.extend([sku, sku])

    where_skus = " OR ".join(conditions)

    query = (
        PRODUCT_STRUCTURE_SELECT.replace(
            "WHERE\n        AND",
            f"WHERE ({where_skus})\n        AND",
        )
        + "\nORDER BY PRODUTO.B1_COD"
    )

    return execute_query(query, tuple(params))


def map_product_structure(product: dict) -> dict:
    """
    Equivalente ao ProductMapper.toStructure do projeto TypeScript.
    """

    def number(value):
        try:
            return float(value or 0)
        except (ValueError, TypeError):
            return 0

    return {
        "B1_COD": str(product.get("B1_COD") or "").strip(),
        "B1_DESC": str(product.get("B1_DESC") or "").strip(),
        "B1_PESO": number(product.get("B1_PESO")),

        "G1_COMP": str(product.get("G1_COMP") or "").strip(),
        "B1_DESC_EMBALAGEM": str(
            product.get("B1_DESC_EMBALAGEM") or ""
        ).strip(),
        "G1_QUANT": number(product.get("G1_QUANT")),

        "B5_LARG": number(product.get("B5_LARG")),
        "B5_COMPR": number(product.get("B5_COMPR")),
        "B5_ESPESS": number(product.get("B5_ESPESS")),
    }


# ============================================================
# REPOSITÓRIO DE NOTAS FISCAIS
# ============================================================

def find_invoices(
    nf: int | None = None,
    cnpj: str | None = None,
    data_inicio: date | None = None,
    data_fim: date | None = None,
    limit: int | None = None,
) -> list[dict]:

    params = []
    top = ""

    if limit is not None:
        top = "TOP (?)"
        params.append(limit)

    query = f"""
        SELECT {top} *
        FROM VIEW_PROTHEUS_GERENCIAL_VENDAS
        WHERE FILIAL = 'POLAR'
    """

    if nf is not None:
        query += """
            AND TRY_CONVERT(BIGINT, NotaFiscal) = ?
        """
        params.append(nf)

    if cnpj:
        query += """
            AND REPLACE(
                REPLACE(
                    REPLACE(
                        REPLACE(CPF_CNPJ, '-', ''),
                        '.',
                        ''
                    ),
                    '/',
                    ''
                ),
                ' ',
                ''
            ) = ?
        """
        params.append(cnpj)

    if data_inicio:
        query += """
            AND DataEmissao >= ?
        """
        params.append(data_inicio)

    if data_fim:
        query += """
            AND DataEmissao < DATEADD(DAY, 1, ?)
        """
        params.append(data_fim)

    query += """
        ORDER BY
            DataEmissao DESC,
            NotaFiscal DESC,
            N_Item ASC
    """

    return execute_query(query, tuple(params))


# ============================================================
# ROTAS DE TESTE
# ============================================================

@app.get(
    "/v1/tcp-check",
    dependencies=[Depends(require_api_token)],
)
def tcp_check():

    start = time.monotonic()

    try:
        with socket.create_connection(
            (SQL_SERVER, SQL_PORT),
            timeout=SQL_CONNECTION_TIMEOUT,
        ):
            elapsed = round(time.monotonic() - start, 3)

            return {
                "stage": "tcp",
                "status": "success",
                "host": SQL_SERVER,
                "port": SQL_PORT,
                "elapsed_seconds": elapsed,
            }

    except OSError as error:
        logger.exception("Falha no teste TCP.")

        return JSONResponse(
            status_code=503,
            content={
                "stage": "tcp",
                "status": "failed",
                "category": "connection_error",
                "error_type": type(error).__name__,
            },
        )


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
        stage = "driver_check"

        drivers = pyodbc.drivers()

        if SQL_DRIVER not in drivers:
            raise RuntimeError(
                f"Driver ODBC não encontrado: {SQL_DRIVER}"
            )

        stage = "connection"

        conn = get_db_connection()
        cursor = conn.cursor()

        stage = "query_execution"

        cursor.execute("SELECT 1 AS result")

        result = cursor.fetchone()

        elapsed = round(time.monotonic() - start, 3)

        return {
            "stage": "completed",
            "status": "success",
            "database": SQL_DATABASE,
            "driver": SQL_DRIVER,
            "query_result": result[0],
            "elapsed_seconds": elapsed,
        }

    except Exception as error:
        logger.exception("DB_CHECK | Falha na etapa %s", stage)

        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "stage": stage,
                    "status": "failed",
                    "category": "internal_error",
                    "error_type": type(error).__name__,
                }
            },
        )

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None:
            conn.close()


@app.get(
    "/v1/test",
    dependencies=[Depends(require_api_token)],
)
def test_sb1(sku: str = Query(..., min_length=1)):

    normalized_sku = normalize_sku(sku)

    query = """
        SELECT TOP 1 *
        FROM SB1G10
        WHERE
            TRIM(B1_COD) = ?
            OR TRY_CONVERT(BIGINT, TRIM(B1_COD)) =
               TRY_CONVERT(BIGINT, ?)
    """

    try:
        result = execute_query(
            query,
            (normalized_sku, normalized_sku),
        )

        return {
            "success": True,
            "data": result[0] if result else None,
        }

    except Exception as error:
        logger.exception("Erro na rota de teste SB1G10.")

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Erro ao consultar SB1G10",
                "error_type": type(error).__name__,
            },
        )


# ============================================================
# ROTAS AUXILIARES DE CONSULTA
# ============================================================

@app.get(
    "/v1/sb1",
    dependencies=[Depends(require_api_token)],
)
def get_sb1(sku: str = Query(..., min_length=1)):

    sku = normalize_sku(sku)

    query = """
        SELECT TOP 1 *
        FROM SB1G10
        WHERE
            TRIM(B1_COD) = ?
            OR TRY_CONVERT(BIGINT, TRIM(B1_COD)) =
               TRY_CONVERT(BIGINT, ?)
    """

    try:
        result = execute_query(query, (sku, sku))

        return {
            "success": True,
            "data": result[0] if result else None,
        }

    except Exception as error:
        logger.exception("Erro ao consultar SB1G10.")

        raise HTTPException(
            status_code=500,
            detail="Erro ao consultar SB1G10.",
        )


@app.get(
    "/v1/sb5",
    dependencies=[Depends(require_api_token)],
)
def get_sb5(sku: str = Query(..., min_length=1)):

    sku = normalize_sku(sku)

    query = """
        SELECT TOP 1 *
        FROM SB5G10
        WHERE
            TRIM(B5_COD) = ?
            OR TRY_CONVERT(BIGINT, TRIM(B5_COD)) =
               TRY_CONVERT(BIGINT, ?)
    """

    try:
        result = execute_query(query, (sku, sku))

        return {
            "success": True,
            "data": result[0] if result else None,
        }

    except Exception:
        logger.exception("Erro ao consultar SB5G10.")

        raise HTTPException(
            status_code=500,
            detail="Erro ao consultar SB5G10.",
        )


@app.get(
    "/v1/sg1",
    dependencies=[Depends(require_api_token)],
)
def get_sg1(sku: str = Query(..., min_length=1)):

    sku = normalize_sku(sku)

    query = """
        SELECT *
        FROM SG1G10
        WHERE
            TRIM(G1_COD) = ?
            OR TRY_CONVERT(BIGINT, TRIM(G1_COD)) =
               TRY_CONVERT(BIGINT, ?)
    """

    try:
        result = execute_query(query, (sku, sku))

        return {
            "success": True,
            "data": result,
        }

    except Exception:
        logger.exception("Erro ao consultar SG1G10.")

        raise HTTPException(
            status_code=500,
            detail="Erro ao consultar SG1G10.",
        )


# ============================================================
# ROTA DE PRODUÇÃO: PRODUCTS
# ============================================================

@app.get(
    "/products",
    dependencies=[Depends(require_api_token)],
)
def get_products(
    sku: str | None = None,
    qtd: float | None = None,
    items: str | None = None,
):
    """
    Modo 1:
        /products?sku=2430

    Modo 2:
        /products?sku=2430&qtd=20

    Modo 3:
        /products?items=[{"sku":"2430","qtd":20}]
    """

    try:
        # ----------------------------------------------------
        # MODO 1: LISTA DE PRODUTOS
        # ----------------------------------------------------

        if items is not None:
            try:
                parsed_items = json.loads(items)

            except json.JSONDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="Parâmetro items inválido. Envie um JSON válido.",
                )

            if not isinstance(parsed_items, list):
                raise HTTPException(
                    status_code=400,
                    detail="O parâmetro items deve ser uma lista.",
                )

            if not parsed_items:
                raise HTTPException(
                    status_code=400,
                    detail="A lista de produtos não pode estar vazia.",
                )

            if len(parsed_items) > 100:
                raise HTTPException(
                    status_code=400,
                    detail="O máximo permitido é 100 produtos por requisição.",
                )

            normalized_items = []

            for index, item in enumerate(parsed_items):
                if not isinstance(item, dict):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Item {index + 1} inválido.",
                    )

                item_sku = normalize_sku(
                    str(item.get("sku") or "")
                )

                try:
                    quantity = float(item.get("qtd"))
                except (ValueError, TypeError):
                    quantity = 0

                if not item_sku:
                    raise HTTPException(
                        status_code=400,
                        detail=f"SKU não informado no item {index + 1}.",
                    )

                if quantity <= 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Quantidade inválida para o SKU {item_sku}.",
                    )

                normalized_items.append({
                    "sku": item_sku,
                    "qtd": quantity,
                })

            products_raw = find_product_structures(
                [item["sku"] for item in normalized_items]
            )

            products = [
                map_product_structure(product)
                for product in products_raw
            ]

            results = []
            not_found = []

            for item in normalized_items:
                product = find_numeric_sku_match(
                    item["sku"],
                    products,
                )

                if not product:
                    not_found.append(item["sku"])

                    results.append({
                        "sku": item["sku"],
                        "qtd": item["qtd"],
                        "success": False,
                        "message": "Produto ou embalagem não encontrada.",
                    })

                    continue

                results.append({
                    "sku": item["sku"],
                    "qtd": item["qtd"],
                    "success": True,
                    "data": product,
                })

            return {
                "success": True,
                "data": {
                    "items": results,
                    "produtosNaoEncontrados": not_found,
                },
                "note": (
                    "A estrutura dos produtos foi consultada. "
                    "O cálculo logístico depende da implementação "
                    "do LogisticsService original."
                ),
            }

        # ----------------------------------------------------
        # MODO 2: SKU ÚNICO
        # ----------------------------------------------------

        normalized_sku = normalize_sku(sku)

        if not normalized_sku:
            raise HTTPException(
                status_code=400,
                detail="SKU não informado.",
            )

        if qtd is not None and qtd <= 0:
            raise HTTPException(
                status_code=400,
                detail="Quantidade inválida.",
            )

        products_raw = find_product_structures(
            [normalized_sku]
        )

        product_raw = find_numeric_sku_match(
            normalized_sku,
            products_raw,
        )

        if not product_raw:
            raise HTTPException(
                status_code=404,
                detail="Produto ou embalagem não encontrada.",
            )

        product = map_product_structure(product_raw)

        if qtd is None:
            return {
                "success": True,
                "data": product,
            }

        return {
            "success": True,
            "data": {
                **product,
                "qtd": qtd,
            },
            "note": (
                "A estrutura foi consultada. "
                "O cálculo logístico depende da implementação "
                "do LogisticsService original."
            ),
        }

    except HTTPException:
        raise

    except Exception as error:
        logger.exception("Erro ao consultar produtos.")

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Erro ao consultar produtos.",
                "error_type": type(error).__name__,
            },
        )


# ============================================================
# ROTA DE PRODUÇÃO: INVOICE
# ============================================================

@app.get(
    "/invoice",
    dependencies=[Depends(require_api_token)],
)
def get_invoices(
    nf: int | None = None,
    cnpj: str | None = None,
    dataInicio: str | None = None,
    dataFim: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    """
    Exemplos:

    /invoice
    /invoice?nf=123456
    /invoice?cnpj=00000000000000
    /invoice?dataInicio=2026-01-01&dataFim=2026-01-31
    /invoice?limit=100
    """

    try:
        start_date = validate_date(
            dataInicio,
            "dataInicio",
        )

        end_date = validate_date(
            dataFim,
            "dataFim",
        )

        if start_date and end_date and start_date > end_date:
            raise HTTPException(
                status_code=400,
                detail="dataInicio não pode ser posterior a dataFim.",
            )

        normalized_cnpj = (
            normalize_cnpj(cnpj)
            if cnpj
            else None
        )

        invoices = find_invoices(
            nf=nf,
            cnpj=normalized_cnpj,
            data_inicio=start_date,
            data_fim=end_date,
            limit=validate_limit(limit),
        )

        return {
            "success": True,
            "total": len(invoices),
            "items": invoices,
        }

    except HTTPException:
        raise

    except Exception as error:
        logger.exception("Erro ao consultar invoices.")

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": "Erro ao consultar notas fiscais.",
                "error_type": type(error).__name__,
            },
        )


# ============================================================
# ROTA DE PRODUÇÃO: INVOICE POR CLIENTE
# ============================================================

@app.get(
    "/invoice/{cnpj}",
    dependencies=[Depends(require_api_token)],
)
def get_customer_invoices(
    cnpj: str,
    dataInicio: str | None = None,
    dataFim: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    """
    Exemplo:

    /invoice/00000000000000

    /invoice/00000000000000?dataInicio=2026-01-01&dataFim=2026-01-31

    /invoice/00000000000000?limit=100
    """

    try:
        normalized_cnpj = normalize_cnpj(cnpj)

        if not normalized_cnpj:
            raise HTTPException(
                status_code=400,
                detail="CNPJ obrigatório.",
            )

        start_date = validate_date(
            dataInicio,
            "dataInicio",
        )

        end_date = validate_date(
            dataFim,
            "dataFim",
        )

        if start_date and end_date and start_date > end_date:
            raise HTTPException(
                status_code=400,
                detail="dataInicio não pode ser posterior a dataFim.",
            )

        invoices = find_invoices(
            cnpj=normalized_cnpj,
            data_inicio=start_date,
            data_fim=end_date,
            limit=validate_limit(limit),
        )

        return {
            "cnpj": normalized_cnpj,
            "total": len(invoices),
            "items": invoices,
        }

    except HTTPException:
        raise

    except Exception as error:
        logger.exception("Erro ao buscar invoices do cliente.")

        raise HTTPException(
            status_code=500,
            detail={
                "message": "Erro interno ao consultar cliente.",
                "error_type": type(error).__name__,
            },
        )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def root():
    return {
        "name": "Polar API",
        "status": "online",
        "version": "1.0.0",
    }
