
import json
import logging
import re

from datetime import date
from decimal import Decimal
from typing import Optional

import pyodbc

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


from app.database import get_connection
from app.auth import require_api_token


logger = logging.getLogger("polar-api")

app = FastAPI(
    title="Polar API",
    version="1.0.0",
)


# ============================================================
# HELPERS
# ============================================================

def rows_to_dict(cursor, rows):
    """
    Converte resultados do pyodbc para uma lista de dicionários.
    """

    columns = [
        column[0]
        for column in cursor.description
    ]

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def serialize_data(data):
    """
    Prepara datas e valores numéricos para resposta JSON.
    """

    return jsonable_encoder(data)


def normalize_cnpj(value: str) -> str:
    return re.sub(r"\D", "", value)


def normalize_sku(value: str) -> str:
    return str(value).strip()


def find_numeric_sku(value: str) -> Optional[int]:
    """
    Retorna o SKU numérico quando possível.
    """

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def root():
    return {
        "success": True,
        "service": "Polar API",
    }


@app.get("/health")
def health():
    return {
        "success": True,
        "status": "online",
    }


# ============================================================
# INVOICE
#
# GET /invoice
#
# Parâmetros:
#   nf
#   cnpj
#   dataInicio
#   dataFim
#   limit
# ============================================================

@app.get(
    "/invoice",
    dependencies=[Depends(require_api_token)],
)
def get_invoices(
    nf: Optional[int] = Query(default=None),
    cnpj: Optional[str] = Query(default=None),
    dataInicio: Optional[date] = Query(default=None),
    dataFim: Optional[date] = Query(default=None),
    limit: Optional[int] = Query(default=None, gt=0, le=5000),
):

    conn = None
    cursor = None

    try:

        if dataInicio and dataFim and dataInicio > dataFim:
            raise HTTPException(
                status_code=400,
                detail="dataInicio não pode ser maior que dataFim",
            )

        normalized_cnpj = None

        if cnpj:
            normalized_cnpj = normalize_cnpj(cnpj)

            if not normalized_cnpj:
                raise HTTPException(
                    status_code=400,
                    detail="CNPJ inválido",
                )

        conn = get_connection()
        cursor = conn.cursor()

        query = """
            SELECT
        """

        params = []

        if limit:
            query += " TOP (?) "
            params.append(limit)

        query += """
                *
            FROM VIEW_PROTHEUS_GERENCIAL_VENDAS
            WHERE FILIAL = 'POLAR'
        """

        # ----------------------------------------------------
        # FILTRO: NOTA FISCAL
        # ----------------------------------------------------

        if nf is not None:

            query += """
                AND TRY_CONVERT(BIGINT, NotaFiscal) = ?
            """

            params.append(nf)

        # ----------------------------------------------------
        # FILTRO: CNPJ
        # ----------------------------------------------------

        if normalized_cnpj:

            query += """
                AND REPLACE(
                    REPLACE(
                        REPLACE(
                            CPF_CNPJ,
                            '-',
                            ''
                        ),
                        '.',
                        ''
                    ),
                    ' ',
                    ''
                ) = ?
            """

            params.append(normalized_cnpj)

        # ----------------------------------------------------
        # FILTRO: DATA INICIAL
        # ----------------------------------------------------

        if dataInicio:

            query += """
                AND DataEmissao >= ?
            """

            params.append(dataInicio)

        # ----------------------------------------------------
        # FILTRO: DATA FINAL
        # ----------------------------------------------------

        if dataFim:

            query += """
                AND DataEmissao < DATEADD(DAY, 1, ?)
            """

            params.append(dataFim)

        # ----------------------------------------------------
        # ORDENAÇÃO
        # ----------------------------------------------------

        query += """
            ORDER BY
                DataEmissao DESC,
                NotaFiscal DESC,
                N_Item ASC
        """

        cursor.execute(query, params)

        rows = cursor.fetchall()

        data = rows_to_dict(
            cursor,
            rows,
        )

        return {
            "success": True,
            "total": len(data),
            "items": serialize_data(data),
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception(
            "INVOICE | Erro ao consultar notas fiscais"
        )

        raise HTTPException(
            status_code=500,
            detail="Erro interno ao consultar notas fiscais",
        )

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


# ============================================================
# PRODUCTS
#
# GET /products?sku=2430
#
# GET /products?sku=2430&qtd=20
#
# GET /products?items=[{"sku":"2430","qtd":20}]
# ============================================================

def fetch_product_structures(skus: list[str]):

    normalized_skus = list(
        dict.fromkeys(
            normalize_sku(sku)
            for sku in skus
            if sku and normalize_sku(sku)
        )
    )

    if not normalized_skus:
        return []

    conn = None
    cursor = None

    try:

        conn = get_connection()
        cursor = conn.cursor()

        conditions = []
        params = []

        for sku in normalized_skus:

            conditions.append(
                """
                (
                    TRIM(PRODUTO.B1_COD) = ?

                    OR

                    TRY_CONVERT(
                        BIGINT,
                        TRIM(PRODUTO.B1_COD)
                    ) = TRY_CONVERT(BIGINT, ?)
                )
                """
            )

            params.extend([
                sku,
                sku,
            ])

        sku_conditions = " OR ".join(conditions)

        query = f"""
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

                (
                    {sku_conditions}
                )

                AND TRIM(
                    ISNULL(ESTRUTURA.D_E_L_E_T_, '')
                ) <> '*'

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

            ORDER BY
                PRODUTO.B1_COD
        """

        cursor.execute(query, params)

        rows = cursor.fetchall()

        return rows_to_dict(
            cursor,
            rows,
        )

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


def find_product_rows(
    sku: str,
    product_rows: list[dict],
):
    """
    Localiza as linhas da estrutura de um SKU.
    Primeiro tenta o código exato.
    Depois compara o valor numérico, permitindo
    equivalência entre 2430 e 002430.
    """

    normalized_sku = normalize_sku(sku)

    exact = [
        row
        for row in product_rows
        if normalize_sku(row.get("B1_COD", "")) == normalized_sku
    ]

    if exact:
        return exact

    numeric_sku = find_numeric_sku(normalized_sku)

    if numeric_sku is None:
        return []

    return [
        row
        for row in product_rows
        if find_numeric_sku(
            normalize_sku(row.get("B1_COD", ""))
        ) == numeric_sku
    ]


# ============================================================
# PRODUCTS ROUTE
# ============================================================

@app.get(
    "/products",
    dependencies=[Depends(require_api_token)],
)
def get_products(
    sku: Optional[str] = Query(default=None),
    qtd: Optional[float] = Query(default=None, gt=0),
    items: Optional[str] = Query(default=None),
):

    try:

        # ====================================================
        # MODO 1: LISTA DE PRODUTOS
        #
        # ?items=[
        #   {"sku":"2430","qtd":20},
        #   {"sku":"1000","qtd":50}
        # ]
        # ====================================================

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

            normalized_items = []

            for index, item in enumerate(parsed_items):

                if not isinstance(item, dict):

                    raise HTTPException(
                        status_code=400,
                        detail=f"Item {index + 1} inválido.",
                    )

                item_sku = normalize_sku(
                    item.get("sku", "")
                )

                if not item_sku:

                    raise HTTPException(
                        status_code=400,
                        detail=f"SKU não informado no item {index + 1}.",
                    )

                try:
                    quantidade = float(item.get("qtd"))
                except (TypeError, ValueError):

                    raise HTTPException(
                        status_code=400,
                        detail=f"Quantidade inválida para o SKU {item_sku}.",
                    )

                if quantidade <= 0:

                    raise HTTPException(
                        status_code=400,
                        detail=f"Quantidade inválida para o SKU {item_sku}.",
                    )

                normalized_items.append({
                    "sku": item_sku,
                    "qtd": quantidade,
                })

            skus = [
                item["sku"]
                for item in normalized_items
            ]

            product_rows = fetch_product_structures(skus)

            results = []

            for item in normalized_items:

                product_data = find_product_rows(
                    item["sku"],
                    product_rows,
                )

                if not product_data:

                    results.append({
                        "sku": item["sku"],
                        "qtd": item["qtd"],
                        "success": False,
                        "message": "Produto ou embalagem não encontrada",
                    })

                    continue

                results.append({
                    "sku": item["sku"],
                    "qtd": item["qtd"],
                    "success": True,
                    "data": product_data,
                })

            not_found = [
                item["sku"]
                for item in results
                if not item["success"]
            ]

            return {
                "success": True,
                "data": {
                    "items": serialize_data(results),
                    "produtosNaoEncontrados": not_found,
                },
            }

        # ====================================================
        # MODO 2: SKU ÚNICO
        #
        # ?sku=2430
        # ?sku=2430&qtd=20
        # ====================================================

        if not sku or not sku.strip():

            raise HTTPException(
                status_code=400,
                detail="SKU não informado",
            )

        normalized_sku = normalize_sku(sku)

        product_rows = fetch_product_structures([
            normalized_sku
        ])

        product_data = find_product_rows(
            normalized_sku,
            product_rows,
        )

        if not product_data:

            raise HTTPException(
                status_code=404,
                detail="Produto ou embalagem não encontrada",
            )

        return {
            "success": True,
            "data": {
                "sku": normalized_sku,
                "qtd": qtd,
                "structure": serialize_data(product_data),
            },
        }

    except HTTPException:
        raise

    except Exception:

        logger.exception(
            "PRODUCTS | Erro ao consultar produtos"
        )

        raise HTTPException(
            status_code=500,
            detail="Erro interno ao consultar produtos",
        )
