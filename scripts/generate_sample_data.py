"""Gera CSVs sintéticos de supermercado para demonstração e testes."""
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TODAY = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
random.seed(42)

PRODUCTS = [
    (1, "Picanha bovina resfriada kg", "carnes", 58.50, 89.90),
    (2, "Filé de peito de frango 1 kg", "carnes", 14.20, 22.90),
    (3, "Carne moída patinho kg", "carnes", 29.80, 42.50),
    (4, "Leite integral 1 L", "laticínios", 3.80, 5.49),
    (5, "Iogurte natural 400 g", "laticínios", 7.10, 11.90),
    (6, "Requeijão tradicional 200 g", "laticínios", 5.90, 9.80),
    (7, "Alface americana un", "hortifrúti", 2.20, 4.50),
    (8, "Tomate italiano kg", "hortifrúti", 4.50, 8.90),
    (9, "Morango bandeja 250 g", "hortifrúti", 6.00, 10.50),
    (10, "Queijo muçarela 200 g", "frios", 7.90, 12.90),
    (11, "Presunto cozido 200 g", "frios", 6.40, 10.90),
    (12, "Salame fatiado 100 g", "frios", 8.50, 14.90),
    (13, "Pão de queijo congelado 1 kg", "padaria", 13.50, 22.90),
    (14, "Bolo de cenoura 400 g", "padaria", 8.00, 15.90),
    (15, "Pão de forma 500 g", "padaria", 5.20, 9.50),
]


def write_csv(name: str, fieldnames: list[str], rows: list[dict]) -> None:
    with (DATA_DIR / name).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    products = [
        {"id": p[0], "nome": p[1], "categoria": p[2], "preco_custo": p[3], "preco_venda": p[4]}
        for p in PRODUCTS
    ]
    write_csv("produtos.csv", ["id", "nome", "categoria", "preco_custo", "preco_venda"], products)

    lots = []
    scenarios = [(-1, 8, 25), (2, 1, 45), (1, 7, 80), (3, 14, 35), (5, 4, 220), (6, 10, 110),
                 (4, 9, 65), (11, 5, 90), (14, 11, 140), (9, 2, 160), (35, 3, 100), (60, 13, 180)]
    for i, (days, product_id, quantity) in enumerate(scenarios, start=99):
        lots.append({
            "id": i,
            "produto_id": product_id,
            "quantidade": quantity,
            "data_entrada": (TODAY - timedelta(days=10)).isoformat(),
            "data_validade": (TODAY + timedelta(days=days)).isoformat(),
        })
    # Cenários intencionais de qualidade de dados; mantidos só para demonstrar o tratamento defensivo.
    lots.extend([
        {"id": 112, "produto_id": 9999, "quantidade": 50, "data_entrada": TODAY.isoformat(),
         "data_validade": (TODAY + timedelta(days=2)).isoformat()},
        {"id": 113, "produto_id": 1, "quantidade": "inválido", "data_entrada": TODAY.isoformat(),
         "data_validade": (TODAY + timedelta(days=2)).isoformat()},
    ])
    write_csv("lotes_estoque.csv", ["id", "produto_id", "quantidade", "data_entrada", "data_validade"], lots)

    averages = {1: 8, 2: 25, 3: 15, 4: 30, 5: 8, 6: 12, 7: 35, 8: 20, 9: 10, 10: 12,
                11: 10, 12: 5, 13: 8, 14: 6, 15: 14}
    sales = []
    row_id = 1
    # 30 dias completos; inclui hoje e datas antigas para exercitar o filtro móvel de sete dias.
    for offset in range(31):
        sale_date = TODAY - timedelta(days=offset)
        for product_id, average in averages.items():
            quantity = max(1, round(random.gauss(average, average * 0.2)))
            sales.append({"id": row_id, "produto_id": product_id, "quantidade": quantity,
                          "data_venda": sale_date.isoformat()})
            row_id += 1
    write_csv("vendas.csv", ["id", "produto_id", "quantidade", "data_venda"], sales)
    print(f"Dados sintéticos gerados em {DATA_DIR} para {TODAY.isoformat()}.")


if __name__ == "__main__":
    main()
