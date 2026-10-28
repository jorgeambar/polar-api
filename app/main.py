import hmac
import os
from typing import Annotated

import pyodbc
from fastapi import FastAPI, Depends, Header, HTTPException, Query

app = FastAPI(
    title="Polar API",
    version="0.1.0",
    description="API de integração Polar"
)


# Autenticação da API
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


# Conexão com SQL Server
def get_connection():
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

    return pyodbc.connect(conn_str, timeout=8)


# Endpoint público
@app.get("/")
def root():
    return {
        "service": "Polar API",
        "status": "ok"
    }


# Monitoramento
@app.get("/health")
def health():
    return {"status": "ok"}


# Teste de conexão com o banco
@app.get(
    "/v1/db-check",
    dependencies=[Depends(require_api_token)]
)
def db_check():
    try:
        with get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT 1"
            ).fetchone()

        return {
            "database": "connected",
            "check": row[0]
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database connection failed"
        )


# Consulta controlada de notas fiscais
@app.get(
    "/v1/invoices",
    dependencies=[Depends(require_api_token)]
)
def invoices(
    nf: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50
):
    sql = """
        SELECT TOP (?)
            *
        FROM VIEW_PROTHEUS_GERENCIAL_VENDAS
        WHERE FILIAL = 'POLAR'
    """

    params = [limit]

    if nf is not None:
        sql += " AND TRY_CONVERT(BIGINT, NotaFiscal) = ?"
        params.append(nf)

    sql += """
        ORDER BY DataEmissao DESC,
                 NotaFiscal DESC,
                 N_Item ASC
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)

            columns = [
                col[0] for col in cursor.description
            ]

            rows = cursor.fetchmany(limit)

        return {
            "items": [
                dict(zip(columns, row))
                for row in rows
            ],
            "count": len(rows)
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Invoice query failed"
        )
