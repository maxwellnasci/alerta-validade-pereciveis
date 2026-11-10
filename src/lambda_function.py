import csv
import io
import json
from html import escape as html_escape
import logging
import os
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import boto3

# Fuso horário padrão do Brasil (Horário de Brasília)
FUSO_SP = ZoneInfo("America/Sao_Paulo")

# Configuração de logging estruturado
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Inicialização dos clientes AWS (utiliza sa-east-1 como padrão)
AWS_REGION = os.environ.get("AWS_REGION", "sa-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)
ses_client = boto3.client("ses", region_name=AWS_REGION)

# Variáveis de ambiente configuráveis
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
REMETENTE_EMAIL = os.environ.get("REMETENTE_EMAIL", "")
DESTINATARIO_EMAIL = os.environ.get("DESTINATARIO_EMAIL", "")


def formatar_moeda_br(valor: float) -> str:
    """Formata valor float estritamente no padrão monetário brasileiro (ex: R$ 1.234,56)."""
    formatado = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatado}"


def ler_csv_s3(bucket: str, chave: str):
    """Lê um arquivo CSV do S3 e retorna uma lista de dicionários."""
    resposta = s3_client.get_object(Bucket=bucket, Key=chave)
    conteudo = resposta["Body"].read().decode("utf-8")
    leitor = csv.DictReader(io.StringIO(conteudo))
    return list(leitor)


def classificar_urgencia(dias_ate_vencer: int) -> str:
    """Classifica a faixa de urgência conforme a proximidade do vencimento."""
    if dias_ate_vencer <= 3:
        return "CRÍTICO"
    elif 4 <= dias_ate_vencer <= 7:
        return "ATENÇÃO"
    elif 8 <= dias_ate_vencer <= 15:
        return "MONITORAR"
    return "SEGURO"


def sugerir_acao(urgencia: str, dias_ate_vencer: int, perda_estimada: float) -> str:
    """Recomenda ação prática para o gerente de loja evitar que a mercadoria expire."""
    if urgencia == "CRÍTICO":
        if perda_estimada > 0:
            return "Liquidação preventiva imediata (desconto de 30% a 50% na ilha de queima)"
        return "Expor em ponta de gôndola para acelerar saída"
    elif urgencia == "ATENÇÃO":
        if perda_estimada > 0:
            return "Acionar troca com fornecedor ou combo promocional (Leve 2 Pague 1)"
        return "Manter monitoramento de giro na abertura da loja"
    elif urgencia == "MONITORAR":
        return "Ajustar próximo pedido de compras no ERP (reduzir volume da ordem)"
    return "Acompanhamento de rotina"


def distribuir_demanda_fefo(lotes: list[dict], medias_diarias: dict[int, float]) -> dict[int, float]:
    """Aloca a demanda diária nos lotes elegíveis em ordem FEFO (validade mais próxima primeiro)."""
    vendidos_por_lote = {lote["_indice"]: 0.0 for lote in lotes}
    por_produto = {}
    for lote in lotes:
        por_produto.setdefault(lote["produto_id"], []).append(lote)

    for produto_id, lotes_produto in por_produto.items():
        maior_prazo = max((lote["dias_ate_vencer"] for lote in lotes_produto), default=0)
        media_diaria = medias_diarias.get(produto_id, 0.0)
        saldos = {lote["_indice"]: float(lote["quantidade"]) for lote in lotes_produto}

        # Simula a demanda média em cada dia futuro. Em cada dia, atende primeiro
        # o lote com vencimento mais próximo, sem reutilizar a mesma venda prevista.
        for dia in range(1, maior_prazo + 1):
            demanda_restante = media_diaria
            ativos = sorted(
            (lote for lote in lotes_produto if lote["dias_ate_vencer"] >= dia),
            key=lambda lote: (lote["data_validade_obj"], lote["lote_id"], lote["_indice"]),
            )
            for lote in ativos:
                if demanda_restante <= 0:
                    break
                indice = lote["_indice"]
                quantidade_vendida = min(saldos[indice], demanda_restante)
                saldos[indice] -= quantidade_vendida
                vendidos_por_lote[indice] += quantidade_vendida
                demanda_restante -= quantidade_vendida

    return vendidos_por_lote


def processar_alerta_validade(hoje: date = None) -> dict:
    """Cruza CSVs, valida dados e estima perdas distribuindo demanda por lotes via FEFO."""
    if hoje is None:
        hoje = datetime.now(FUSO_SP).date()

    linhas_ignoradas = 0
    logger.info("Lendo arquivos CSV do bucket %s...", BUCKET_NAME)
    produtos_raw = ler_csv_s3(BUCKET_NAME, "dados/produtos.csv")
    lotes_raw = ler_csv_s3(BUCKET_NAME, "dados/lotes_estoque.csv")
    vendas_raw = ler_csv_s3(BUCKET_NAME, "dados/vendas.csv")

    produtos = {}
    for p in produtos_raw:
        try:
            prod_id = int(p["id"])
            produtos[prod_id] = {
                "nome": p["nome"].strip(),
                "categoria": p["categoria"].strip(),
                "preco_custo": float(p["preco_custo"]),
                "preco_venda": float(p["preco_venda"]),
            }
        except Exception as err:
            linhas_ignoradas += 1
            logger.warning("[AVISO] Linha de produto corrompida ignorada: %s. Erro: %s", p, err)

    data_limite_inicio = hoje - timedelta(days=7)
    data_limite_fim = hoje - timedelta(days=1)
    logger.info("Filtrando histórico de vendas entre %s e %s...", data_limite_inicio, data_limite_fim)
    vendas_por_produto = {}
    for v in vendas_raw:
        try:
            prod_id = int(v["produto_id"])
            qtd = int(v["quantidade"])
            data_v = datetime.strptime(v["data_venda"].strip(), "%Y-%m-%d").date()
            if data_limite_inicio <= data_v <= data_limite_fim:
                vendas_por_produto[prod_id] = vendas_por_produto.get(prod_id, 0) + qtd
        except Exception as err:
            linhas_ignoradas += 1
            logger.warning("[AVISO] Linha de venda inválida ignorada: %s. Erro: %s", v, err)

    medias_diarias = {
        prod_id: vendas_por_produto.get(prod_id, 0) / 7.0
        for prod_id in produtos
    }

    lotes_validos = []
    for indice, l in enumerate(lotes_raw):
        try:
            lote_id = int(l["id"])
            prod_id = int(l["produto_id"])
            quantidade = int(l["quantidade"])
            data_validade = datetime.strptime(l["data_validade"].strip(), "%Y-%m-%d").date()
        except Exception as err:
            linhas_ignoradas += 1
            logger.warning("[AVISO] Linha de lote mal formatada ignorada: %s. Erro: %s", l, err)
            continue

        if prod_id not in produtos:
            linhas_ignoradas += 1
            logger.warning("[AVISO] Lote #%s ignorado: produto_id %s inexistente no cadastro.", lote_id, prod_id)
            continue

        lotes_validos.append({
            "_indice": indice,
            "lote_id": lote_id,
            "produto_id": prod_id,
            "quantidade": quantidade,
            "data_validade_obj": data_validade,
            "dias_ate_vencer": (data_validade - hoje).days,
        })

    lotes_vencidos = []
    lotes_em_risco = []
    total_ja_vencido = 0.0
    for lote in lotes_validos:
        if lote["dias_ate_vencer"] < 0:
            prod = produtos[lote["produto_id"]]
            valor_vencido = lote["quantidade"] * prod["preco_custo"]
            total_ja_vencido += valor_vencido
            lotes_vencidos.append({
                "lote_id": lote["lote_id"], "produto_id": lote["produto_id"],
                "produto_nome": prod["nome"], "categoria": prod["categoria"],
                "quantidade": lote["quantidade"],
                "data_validade": lote["data_validade_obj"].strftime("%d/%m/%Y"),
                "dias_vencido": abs(lote["dias_ate_vencer"]),
                "custo_unit": prod["preco_custo"], "valor_vencido": valor_vencido,
            })
        elif lote["dias_ate_vencer"] <= 15:
            lotes_em_risco.append(lote)

    vendas_previstas = distribuir_demanda_fefo(lotes_em_risco, medias_diarias)
    lotes_analisados = []
    total_valor_em_risco = 0.0
    total_perda_estimada = 0.0

    for lote in lotes_em_risco:
        prod = produtos[lote["produto_id"]]
        custo_unit = prod["preco_custo"]
        quantidade = lote["quantidade"]
        venda_esperada = vendas_previstas[lote["_indice"]]
        valor_em_risco = quantidade * custo_unit
        perda_estimada = max(0.0, quantidade - venda_esperada) * custo_unit
        urgencia = classificar_urgencia(lote["dias_ate_vencer"])
        acao = sugerir_acao(urgencia, lote["dias_ate_vencer"], perda_estimada)
        total_valor_em_risco += valor_em_risco
        total_perda_estimada += perda_estimada
        lotes_analisados.append({
            "lote_id": lote["lote_id"], "produto_id": lote["produto_id"],
            "produto_nome": prod["nome"], "categoria": prod["categoria"],
            "quantidade": quantidade,
            "data_validade": lote["data_validade_obj"].strftime("%d/%m/%Y"),
            "dias_ate_vencer": lote["dias_ate_vencer"], "custo_unit": custo_unit,
            "valor_em_risco": valor_em_risco,
            "media_diaria": medias_diarias.get(lote["produto_id"], 0.0),
            "venda_esperada": venda_esperada,
            "perda_estimada": perda_estimada, "urgencia": urgencia,
            "acao_sugerida": acao,
        })

    # Ordena pela data real; "data_validade" é texto dd/mm/aaaa e não ordena cronologicamente.
    lotes_vencidos.sort(
        key=lambda x: (datetime.strptime(x["data_validade"], "%d/%m/%Y").date(), x["lote_id"])
    )
    lotes_ordenados = sorted(
        lotes_analisados,
        key=lambda x: (x["perda_estimada"], x["valor_em_risco"]),
        reverse=True,
    )
    data_limite_inicio = hoje - timedelta(days=7)
    data_limite_fim = hoje - timedelta(days=1)
    return {
        "data_execucao": hoje.strftime("%d/%m/%Y"),
        "janela_vendas": f"{data_limite_inicio.strftime('%d/%m')} a {data_limite_fim.strftime('%d/%m')}",
        "total_lotes_risco": len(lotes_analisados),
        "total_valor_em_risco": total_valor_em_risco,
        "total_perda_estimada": total_perda_estimada,
        "total_ja_vencido": total_ja_vencido,
        "lotes_vencidos": lotes_vencidos,
        "top_5_lotes": lotes_ordenados[:5],
        "todos_lotes": lotes_analisados,
        "linhas_ignoradas": linhas_ignoradas,
    }

def gerar_email_html(relatorio: dict) -> str:
    """Gera o corpo do e-mail com layout executivo, seção de produtos vencidos e alerta de qualidade de dados."""
    # 1. Tabela dos 5 lotes com maior perda estimada
    linhas_top_5 = ""
    for lote in relatorio["top_5_lotes"]:
        cor_badge = "#D32F2F" if lote["urgencia"] == "CRÍTICO" else "#F57C00" if lote["urgencia"] == "ATENÇÃO" else "#1976D2"
        fundo_badge = "#FFEBEE" if lote["urgencia"] == "CRÍTICO" else "#FFF3E0" if lote["urgencia"] == "ATENÇÃO" else "#E3F2FD"

        linhas_top_5 += f"""
        <tr style="border-bottom: 1px solid #E5E5EA;">
          <td style="padding: 12px; font-weight: 600; color: #1D1D1F;">
            {html_escape(str(lote['produto_nome']))}<br/>
            <span style="font-size: 11px; color: #86868B; text-transform: uppercase;">{html_escape(str(lote['categoria']))} • Lote #{html_escape(str(lote['lote_id']))}</span>
          </td>
          <td style="padding: 12px; text-align: center; color: #1D1D1F;">
            {html_escape(str(lote['quantidade']))} un<br/>
            <span style="font-size: 11px; color: #86868B;">(vence {html_escape(str(lote['data_validade']))})</span>
          </td>
          <td style="padding: 12px; text-align: center;">
            <span style="display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; color: {cor_badge}; background-color: {fundo_badge};">
              {html_escape(str(lote['urgencia']))} ({html_escape(str(lote['dias_ate_vencer']))}d)
            </span>
          </td>
          <td style="padding: 12px; text-align: right; font-weight: 600; color: #D32F2F;">
            {formatar_moeda_br(lote['perda_estimada'])}<br/>
            <span style="font-size: 11px; color: #86868B;">Custo total: {formatar_moeda_br(lote['valor_em_risco'])}</span>
          </td>
          <td style="padding: 12px; font-size: 12px; color: #48484A;">
            {html_escape(str(lote['acao_sugerida']))}
          </td>
        </tr>
        """

    # 2. Seção de Lotes Já Vencidos (se houver algum)
    secao_vencidos = ""
    if relatorio["lotes_vencidos"]:
        linhas_vencidos = ""
        for v in relatorio["lotes_vencidos"]:
            linhas_vencidos += f"""
            <tr style="border-bottom: 1px solid #FFCDD2; background-color: #FFF8F8;">
              <td style="padding: 10px 12px; font-weight: 600; color: #B71C1C;">
                {html_escape(str(v['produto_nome']))} <span style="font-size: 11px; color: #86868B;">(Lote #{html_escape(str(v['lote_id']))})</span>
              </td>
              <td style="padding: 10px 12px; text-align: center; color: #B71C1C;">
                {html_escape(str(v['quantidade']))} un
              </td>
              <td style="padding: 10px 12px; text-align: center; color: #B71C1C; font-weight: bold;">
                Venceu em {html_escape(str(v['data_validade']))} (há {html_escape(str(v['dias_vencido']))}d)
              </td>
              <td style="padding: 10px 12px; text-align: right; font-weight: 700; color: #B71C1C;">
                {formatar_moeda_br(v['valor_vencido'])}
              </td>
            </tr>
            """

        secao_vencidos = f"""
        <div style="margin: 0 32px 20px 32px; padding: 16px 20px; background-color: #FFEBEE; border: 2px solid #EF5350; border-radius: 12px;">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;">
            <div style="font-size: 14px; font-weight: 800; color: #B71C1C; text-transform: uppercase; letter-spacing: 0.5px;">
              ⚠️ Já vencidos — retirar hoje da gôndola
            </div>
            <div style="font-size: 13px; font-weight: 700; color: #B71C1C;">
              Total Quebra: {formatar_moeda_br(relatorio['total_ja_vencido'])}
            </div>
          </div>
          <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
            <thead>
              <tr style="color: #7F0000; text-align: left; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #FFCDD2;">
                <th style="padding: 8px 12px;">Produto / Lote</th>
                <th style="padding: 8px 12px; text-align: center;">Quantidade</th>
                <th style="padding: 8px 12px; text-align: center;">Data Vencimento</th>
                <th style="padding: 8px 12px; text-align: right;">Prejuízo Custo</th>
              </tr>
            </thead>
            <tbody>
              {linhas_vencidos}
            </tbody>
          </table>
        </div>
        """

    # 3. Aviso condicional de qualidade de dados (linhas ignoradas)
    aviso_linhas_ignoradas = ""
    if relatorio.get("linhas_ignoradas", 0) > 0:
        qtd_ign = relatorio["linhas_ignoradas"]
        aviso_linhas_ignoradas = f"""
        <div style="margin: 0 32px 16px 32px; padding: 12px 16px; background-color: #FFF9C4; border-left: 4px solid #FBC02D; border-radius: 6px; font-size: 12px; color: #7F6000;">
          ⚠️ <strong>Aviso de Qualidade de Dados:</strong> {html_escape(str(qtd_ign))} linha(s) ignorada(s) por erro de dados — verificar no log.
        </div>
        """

    # 4. Montagem do HTML Geral
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #F5F5F7; margin: 0; padding: 24px;">
      <div style="max-width: 800px; margin: 0 auto; background-color: #FFFFFF; border-radius: 16px; border: 1px solid #E5E5EA; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        
        <!-- Header -->
        <div style="background-color: #1D1D1F; padding: 24px 32px; color: #FFFFFF;">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
              <span style="display: inline-block; padding: 4px 10px; background-color: #0066CC; border-radius: 100px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;">
                Alerta Diário de Validade • 07h00 (Brasília)
              </span>
              <h1 style="margin: 8px 0 0 0; font-size: 22px; font-weight: 700;">Painel de Risco de Perecíveis</h1>
              <p style="margin: 4px 0 0 0; font-size: 13px; color: #A1A1A6;">Operação demonstrativa • Base de vendas: {html_escape(str(relatorio['janela_vendas']))}</p>
            </div>
            <div style="text-align: right; font-size: 12px; color: #A1A1A6;">
              Data: <strong style="color: #FFFFFF;">{html_escape(str(relatorio['data_execucao']))}</strong>
            </div>
          </div>
        </div>

        <!-- Seção de Alerta Vermelho de Lotes Vencidos (se existirem) -->
        {secao_vencidos}

        <!-- Aviso de Linhas Ignoradas (se houver alguma) -->
        {aviso_linhas_ignoradas}

        <!-- Cards de Resumo Executivo (KPIs) -->
        <div style="padding: 24px 32px; background-color: #FAFAFC; border-bottom: 1px solid #E5E5EA;">
          <table style="width: 100%; border-collapse: collapse;">
            <tr>
              <td style="width: 33%; padding: 12px; background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 12px;">
                <div style="font-size: 11px; color: #86868B; text-transform: uppercase; font-weight: 600;">Lotes Próximos do Vencimento</div>
                <div style="font-size: 22px; font-weight: 700; color: #1D1D1F; margin-top: 4px;">{relatorio['total_lotes_risco']} lotes</div>
                <div style="font-size: 11px; color: #86868B; margin-top: 2px;">Janela de 0 a 15 dias</div>
              </td>
              <td style="width: 2%;"></td>
              <td style="width: 33%; padding: 12px; background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 12px;">
                <div style="font-size: 11px; color: #86868B; text-transform: uppercase; font-weight: 600;">Valor Total em Risco (Custo)</div>
                <div style="font-size: 22px; font-weight: 700; color: #1D1D1F; margin-top: 4px;">{formatar_moeda_br(relatorio['total_valor_em_risco'])}</div>
                <div style="font-size: 11px; color: #86868B; margin-top: 2px;">Custo do estoque exposto</div>
              </td>
              <td style="width: 2%;"></td>
              <td style="width: 33%; padding: 12px; background: #FFFFFF; border: 1px solid #FFCDD2; border-radius: 12px;">
                <div style="font-size: 11px; color: #D32F2F; text-transform: uppercase; font-weight: 700;">Perda Financeira Prevista</div>
                <div style="font-size: 22px; font-weight: 700; color: #D32F2F; margin-top: 4px;">{formatar_moeda_br(relatorio['total_perda_estimada'])}</div>
                <div style="font-size: 11px; color: #D32F2F; margin-top: 2px;">Após giro diário (7 dias)</div>
              </td>
            </tr>
          </table>
        </div>

        <!-- Tabela dos 5 Lotes com Maior Perda Estimada -->
        <div style="padding: 24px 32px;">
          <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px;">
            <h2 style="font-size: 16px; font-weight: 700; color: #1D1D1F; margin: 0;">
              Top 5 Lotes com Maior Perda Financeira Estimada
            </h2>
            <span style="font-size: 11px; color: #86868B;">Ordenado por perda estimada</span>
          </div>

          <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
              <tr style="background-color: #F5F5F7; color: #6E6E73; text-align: left; font-size: 11px; text-transform: uppercase;">
                <th style="padding: 10px 12px;">Produto / Lote</th>
                <th style="padding: 10px 12px; text-align: center;">Estoque</th>
                <th style="padding: 10px 12px; text-align: center;">Status</th>
                <th style="padding: 10px 12px; text-align: right;">Perda Prevista</th>
                <th style="padding: 10px 12px;">Ação Sugerida</th>
              </tr>
            </thead>
            <tbody>
              {linhas_top_5}
            </tbody>
          </table>
        </div>

        <!-- Rodapé -->
        <div style="background-color: #F5F5F7; padding: 16px 32px; border-top: 1px solid #E5E5EA; font-size: 11px; color: #86868B; text-align: center;">
          Demonstração educacional • Automação Serverless AWS • Horário de Brasília
        </div>

      </div>
    </body>
    </html>
    """
    return html


def enviar_email_falha(erro_resumo: str, data_execucao: date):
    """Envia um aviso conciso; detalhes técnicos completos permanecem apenas no CloudWatch."""
    agora_sp = datetime.now(FUSO_SP)
    assunto = f"⚠️ FALHA no alerta de validade ({data_execucao.strftime('%d/%m/%Y')})"
    resumo_seguro = html_escape(str(erro_resumo))
    corpo_html = f"""
    <html>
      <body style="font-family: sans-serif; padding: 20px; color: #1D1D1F;">
        <h2 style="color: #D32F2F;">Falha no alerta diário de validade</h2>
        <p><strong>Data/hora:</strong> {agora_sp.strftime('%d/%m/%Y %H:%M:%S (Horário de Brasília)')}</p>
        <p><strong>Resumo do erro:</strong> <code>{resumo_seguro}</code></p>
      </body>
    </html>
    """
    try:
        ses_client.send_email(
            Source=REMETENTE_EMAIL,
            Destination={"ToAddresses": [DESTINATARIO_EMAIL]},
            Message={
                "Subject": {"Data": assunto, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": corpo_html, "Charset": "UTF-8"},
                    "Text": {"Data": f"Falha no alerta de validade. Resumo do erro: {erro_resumo}", "Charset": "UTF-8"},
                },
            },
        )
        logger.info("E-mail de notificação de falha enviado.")
    except Exception as ses_err:
        logger.error("Erro adicional ao tentar enviar e-mail de falha: %s", ses_err)

def lambda_handler(event, context):
    """Entry point chamado pelo AWS EventBridge ou manualmente."""
    # Data e hora estritamente calibradas para o Fuso Horário de Brasília
    agora_brasilia = datetime.now(FUSO_SP)
    data_hoje = agora_brasilia.date()

    if not BUCKET_NAME or not REMETENTE_EMAIL or not DESTINATARIO_EMAIL:
        raise RuntimeError("Configure BUCKET_NAME, REMETENTE_EMAIL e DESTINATARIO_EMAIL antes de executar.")

    logger.info("Iniciando processamento do Alerta Diário de Validade em %s (%s)...", agora_brasilia.isoformat(), data_hoje)

    try:
        # 1. Processamento e regras de negócio com cruzamento defensivo
        relatorio = processar_alerta_validade(data_hoje)
        logger.info(
            "Processamento concluído: %s lotes em risco (%s em perda prevista), %s lotes vencidos e %s linhas ignoradas.",
            relatorio["total_lotes_risco"],
            formatar_moeda_br(relatorio["total_perda_estimada"]),
            len(relatorio["lotes_vencidos"]),
            relatorio["linhas_ignoradas"],
        )

        # 2. Gerar corpo do e-mail em HTML
        corpo_html = gerar_email_html(relatorio)

        # 3. Assunto executivo com valores formatados em R$ brasileiro
        assunto = (
            f"🚨 Alerta de Validade ({html_escape(str(relatorio['data_execucao']))}): "
            f"{formatar_moeda_br(relatorio['total_perda_estimada'])} em risco iminente"
        )

        # 4. Texto alternativo puro
        texto_linhas_ignoradas = (
            f"\n⚠️ {relatorio['linhas_ignoradas']} linha(s) ignorada(s) por erro de dados — verificar no log."
            if relatorio["linhas_ignoradas"] > 0 else ""
        )

        texto_puro = (
            f"Alerta Diário de Validade ({html_escape(str(relatorio['data_execucao']))} - Horário de Brasília)\n"
            f"Lotes em Risco: {relatorio['total_lotes_risco']}\n"
            f"Valor Total em Risco: {formatar_moeda_br(relatorio['total_valor_em_risco'])}\n"
            f"Perda Prevista: {formatar_moeda_br(relatorio['total_perda_estimada'])}\n"
            f"Lotes Já Vencidos: {len(relatorio['lotes_vencidos'])} ({formatar_moeda_br(relatorio['total_ja_vencido'])})"
            f"{texto_linhas_ignoradas}"
        )

        # 5. Envio pelo Amazon SES
        resposta_ses = ses_client.send_email(
            Source=REMETENTE_EMAIL,
            Destination={"ToAddresses": [DESTINATARIO_EMAIL]},
            Message={
                "Subject": {"Data": assunto, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": corpo_html, "Charset": "UTF-8"},
                    "Text": {"Data": texto_puro, "Charset": "UTF-8"}
                }
            }
        )

        message_id = resposta_ses["MessageId"]
        logger.info("E-mail diário entregue com sucesso via SES! MessageId: %s", message_id)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "message_id": message_id,
                "data_brasilia": relatorio["data_execucao"],
                "total_valor_em_risco_br": formatar_moeda_br(relatorio["total_valor_em_risco"]),
                "total_perda_estimada_br": formatar_moeda_br(relatorio["total_perda_estimada"]),
                "lotes_vencidos_qtd": len(relatorio["lotes_vencidos"]),
                "linhas_ignoradas_qtd": relatorio["linhas_ignoradas"],
                "top_5_lotes": len(relatorio["top_5_lotes"])
            }, ensure_ascii=False)
        }

    except Exception as exc:
        erro_str = str(exc)
        tb_str = traceback.format_exc()
        logger.error("[ERRO CRÍTICO] Falha fatal no processamento: %s", tb_str)

        # Dispara e-mail de contingência com horário de Brasília
        enviar_email_falha(erro_str, data_hoje)

        # Re-lança a exceção para que o CloudWatch registre a métrica Lambda Errors e acione o alarme
        raise exc


if __name__ == "__main__":
    # Teste local simulando a execução da Lambda
    res = lambda_handler(None, None)
    print("\nResultado do teste local:")
    print(json.dumps(json.loads(res["body"]), indent=2, ensure_ascii=False))
