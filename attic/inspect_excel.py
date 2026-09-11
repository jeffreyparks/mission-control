
import pandas as pd
import sys

excel_path = sys.argv[1]
df = pd.read_excel(excel_path)

print("=" * 60)
print("ORG/ROLES TRACKER STRUCTURE")
print("=" * 60)
print(f"\nRows: {df.shape[0]}")
print(f"Columns: {df.shape[1]}")

print("\n" + "=" * 60)
print("COLUMNS:")
print("=" * 60)
for i, col in enumerate(df.columns, 1):
    print(f"{i}. {col}")

print("\n" + "=" * 60)
print("SAMPLE DATA (first 3 rows):")
print("=" * 60)
print(df.head(3).to_string(max_colwidth=40))

print("\n" + "=" * 60)
print("DATA TYPES:")
print("=" * 60)
for col in df.columns:
    print(f"{col}: {df[col].dtype}")
