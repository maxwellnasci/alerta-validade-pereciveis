import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import lambda_function as app  # noqa: E402


def synthetic_data(today: date):
    products = [
        {"id": "1", "nome": "Produto A", "categoria": "teste", "preco_custo": "10.00", "preco_venda": "15.00"},
        {"id": "2", "nome": "Produto B", "categoria": "teste", "preco_custo": "20.00", "preco_venda": "30.00"},
    ]
    lots = [
        {"id": "1", "produto_id": "1", "quantidade": "20", "data_entrada": "2026-01-01",
         "data_validade": (today + timedelta(days=2)).isoformat()},
        {"id": "2", "produto_id": "2", "quantidade": "4", "data_entrada": "2026-01-01",
         "data_validade": (today - timedelta(days=1)).isoformat()},
        {"id": "3", "produto_id": "999", "quantidade": "3", "data_entrada": "2026-01-01",
         "data_validade": (today + timedelta(days=1)).isoformat()},
        {"id": "4", "produto_id": "1", "quantidade": "oops", "data_entrada": "2026-01-01",
         "data_validade": (today + timedelta(days=1)).isoformat()},
    ]
    sales = [
        {"id": "1", "produto_id": "1", "quantidade": "7", "data_venda": (today - timedelta(days=7)).isoformat()},
        {"id": "2", "produto_id": "1", "quantidade": "14", "data_venda": (today - timedelta(days=1)).isoformat()},
        {"id": "3", "produto_id": "1", "quantidade": "700", "data_venda": (today - timedelta(days=8)).isoformat()},
        {"id": "4", "produto_id": "1", "quantidade": "900", "data_venda": today.isoformat()},
        {"id": "5", "produto_id": "1", "quantidade": "not-a-number", "data_venda": today.isoformat()},
    ]
    return products, lots, sales


def test_processa_faixa_sete_dias_e_contabiliza_linhas_invalidas():
    today = date(2026, 11, 7)
    products, lots, sales = synthetic_data(today)

    def fake_read(_bucket, key):
        return {
            "dados/produtos.csv": products,
            "dados/lotes_estoque.csv": lots,
            "dados/vendas.csv": sales,
        }[key]

    with patch.object(app, "ler_csv_s3", side_effect=fake_read):
        report = app.processar_alerta_validade(today)

    assert report["janela_vendas"] == "31/10 a 06/11"
    assert report["todos_lotes"][0]["media_diaria"] == 3.0  # (7 + 14) / 7
    assert len(report["lotes_vencidos"]) == 1
    assert report["lotes_vencidos"][0]["lote_id"] == 2
    assert report["linhas_ignoradas"] == 3


def test_demanda_prevista_e_distribuida_uma_vez_entre_lotes_fefo():
    today = date(2026, 11, 7)
    products = [{"id": "1", "nome": "Produto fictício", "categoria": "teste",
                 "preco_custo": "10.00", "preco_venda": "15.00"}]
    # Ordem de entrada invertida de propósito; o lote #1 vence primeiro e deve receber a venda prevista.
    lots = [
        {"id": "2", "produto_id": "1", "quantidade": "50", "data_entrada": "2026-11-01",
         "data_validade": "2026-11-08"},
        {"id": "1", "produto_id": "1", "quantidade": "50", "data_entrada": "2026-11-01",
         "data_validade": "2026-11-08"},
    ]
    sales = [{"id": str(i), "produto_id": "1", "quantidade": "50",
              "data_venda": (today - timedelta(days=1)).isoformat()} for i in range(7)]

    def fake_read(_bucket, key):
        return {"dados/produtos.csv": products, "dados/lotes_estoque.csv": lots,
                "dados/vendas.csv": sales}[key]

    with patch.object(app, "ler_csv_s3", side_effect=fake_read):
        report = app.processar_alerta_validade(today)

    by_id = {lot["lote_id"]: lot for lot in report["todos_lotes"]}
    assert by_id[1]["venda_esperada"] == 50
    assert by_id[1]["perda_estimada"] == 0
    assert by_id[2]["venda_esperada"] == 0
    assert by_id[2]["perda_estimada"] == 500
    assert report["total_perda_estimada"] == 500


def test_formatacao_brasileira():
    assert app.formatar_moeda_br(1234.56) == "R$ 1.234,56"


def test_html_escapa_textos_dos_dados():
    report = {
        "top_5_lotes": [{
            "produto_nome": "<script>alert(1)</script>", "categoria": "<img src=x>", "lote_id": 1,
            "quantidade": 2, "data_validade": "10/11/2026", "urgencia": "CRÍTICO",
            "dias_ate_vencer": 1, "perda_estimada": 20, "valor_em_risco": 20,
            "acao_sugerida": "<b>perigo</b>",
        }],
        "lotes_vencidos": [], "linhas_ignoradas": 0, "data_execucao": "07/11/2026",
        "janela_vendas": "31/10 a 06/11", "total_lotes_risco": 1,
        "total_valor_em_risco": 20, "total_perda_estimada": 20,
    }
    html = app.gerar_email_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x&gt;" in html
    assert "&lt;b&gt;perigo&lt;/b&gt;" in html


def test_email_de_falha_envia_apenas_resumo_sem_traceback():
    with patch.object(app.ses_client, "send_email") as send_email:
        app.enviar_email_falha("Erro <script>resumo</script>", date(2026, 11, 7))
    message = send_email.call_args.kwargs["Message"]
    html = message["Body"]["Html"]["Data"]
    text = message["Body"]["Text"]["Data"]
    assert "&lt;script&gt;resumo&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "traceback" not in html.lower()
    assert "traceback" not in text.lower()


def test_aviso_de_qualidade_aparece_somente_se_existirem_linhas_ignoradas():
    report = {
        "top_5_lotes": [], "lotes_vencidos": [], "total_ja_vencido": 0,
        "linhas_ignoradas": 2, "data_execucao": "07/11/2026", "janela_vendas": "31/10 a 06/11",
        "total_lotes_risco": 0, "total_valor_em_risco": 0, "total_perda_estimada": 0,
    }
    assert "2 linha(s) ignorada(s)" in app.gerar_email_html(report)
    report["linhas_ignoradas"] = 0
    assert "Aviso de Qualidade de Dados" not in app.gerar_email_html(report)
