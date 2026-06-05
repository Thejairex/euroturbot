import argparse
import json
from pathlib import Path

import pandas as pd
from tabulate import tabulate

FILEPATH = Path(__file__).resolve().parent.parent / "Datos TK 813690.xlsx"


def list_sheets():
    xls = pd.ExcelFile(FILEPATH)
    for s in xls.sheet_names:
        print(f"  {s}")


def load_sheet(sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(FILEPATH, sheet_name=sheet_name, dtype=str, header=None)
    headers = df.iloc[2].tolist()
    data = df.iloc[3:].copy()
    data.columns = headers
    data = data.dropna(axis=1, how="all")
    data = data.loc[:, data.columns.notna()]
    data = data.reset_index(drop=True)
    return data


def show_all(sheet_name: str, format: str = "table"):
    df = load_sheet(sheet_name)
    if format == "json":
        print(df.to_json(orient="records", indent=2, force_ascii=False))
    else:
        print(tabulate(df, headers="keys", tablefmt="grid", showindex=True, maxcolwidths=30))
        print(f"\nTotal: {len(df)} filas x {len(df.columns)} columnas")


def show_row(sheet_name: str, row: int, format: str = "table"):
    df = load_sheet(sheet_name)
    if row < 0 or row >= len(df):
        print(f"Error: fila {row} fuera de rango (0-{len(df)-1})")
        return
    row_data = df.iloc[row].to_dict()
    if format == "json":
        print(json.dumps(row_data, indent=2, ensure_ascii=False))
    else:
        table = [[k, v] for k, v in row_data.items()]
        print(tabulate(table, headers=["Columna", "Valor"], tablefmt="grid"))


def show_column(sheet_name: str, column: str, format: str = "table"):
    df = load_sheet(sheet_name)
    if column not in df.columns:
        print(f"Error: columna '{column}' no encontrada")
        print(f"Columnas disponibles: {list(df.columns)}")
        return
    col_data = df[[column]].copy()
    col_data.index.name = "idx"
    col_data = col_data.reset_index()
    if format == "json":
        print(col_data.to_json(orient="records", indent=2, force_ascii=False))
    else:
        print(tabulate(col_data, headers="keys", tablefmt="grid", showindex=False))
        non_null = col_data[column].notna().sum()
        print(f"\nTotal: {non_null} valores no vacíos")


def show_cell(sheet_name: str, row: int, column: str, format: str = "table"):
    df = load_sheet(sheet_name)
    if row < 0 or row >= len(df):
        print(f"Error: fila {row} fuera de rango (0-{len(df)-1})")
        return
    if column not in df.columns:
        print(f"Error: columna '{column}' no encontrada")
        return
    value = df.iloc[row][column]
    if format == "json":
        print(json.dumps({column: value}, indent=2, ensure_ascii=False))
    else:
        print(f"[{row}] {column} = {value}")


def find_rows(sheet_name: str, column: str, value: str, format: str = "table"):
    df = load_sheet(sheet_name)
    if column not in df.columns:
        print(f"Error: columna '{column}' no encontrada")
        return
    mask = df[column].astype(str).str.strip().str.lower() == value.strip().lower()
    result = df[mask]
    if format == "json":
        print(result.to_json(orient="records", indent=2, force_ascii=False))
    else:
        if result.empty:
            print(f"No se encontraron filas con {column} = '{value}'")
        else:
            print(tabulate(result, headers="keys", tablefmt="grid", showindex=True, maxcolwidths=30))
            print(f"\nTotal: {len(result)} filas")


def main():
    parser = argparse.ArgumentParser(description="Consulta de datos del Excel")
    parser.add_argument("--sheets", action="store_true", help="Listar hojas disponibles")
    parser.add_argument("--sheet", type=str, default="NO TRANS", help="Nombre de la hoja")
    parser.add_argument("--all", action="store_true", help="Mostrar todas las filas")
    parser.add_argument("--row", type=int, help="Número de fila (0-indexed)")
    parser.add_argument("--column", type=str, help="Nombre de columna")
    parser.add_argument("--find", type=str, help="Buscar filas donde columna = valor")
    parser.add_argument("--value", type=str, help="Valor a buscar en --find")
    parser.add_argument("--format", type=str, choices=["table", "json"], default="table", help="Formato de salida")

    args = parser.parse_args()

    if not FILEPATH.exists():
        print(f"Error: no se encuentra {FILEPATH}")
        return

    if args.sheets:
        list_sheets()
        return

    if args.all:
        show_all(args.sheet, args.format)
    elif args.row is not None and args.column:
        show_cell(args.sheet, args.row, args.column, args.format)
    elif args.row is not None:
        show_row(args.sheet, args.row, args.format)
    elif args.column and args.find is not None and args.value is not None:
        find_rows(args.sheet, args.find, args.value, args.format)
    elif args.column:
        show_column(args.sheet, args.column, args.format)
    elif args.find and args.value:
        find_rows(args.sheet, args.find, args.value, args.format)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
