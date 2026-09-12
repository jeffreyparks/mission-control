
import sys, os, pathlib, tempfile, pandas as pd, datetime, shutil, numpy as np
# add uv site-packages for openpyxl
sys.path.append(r"/Users/jeff/.local/share/uv/python/cpython-3.11-macos-aarch64-none/lib/python311/site-packages")
# ensure store module is importable
sys.path.insert(0, r"/Users/jeff/Dev/jeffreyparks/mission-control/agents")
from store import Store, COLUMN_MAP

failures = []

def check(name, cond, msg=''):
    if cond:
        print(f"{name}: ok")
    else:
        print(f"{name}: FAIL {msg}")
        failures.append(name)

def make_store(df):
    base = tempfile.mkdtemp()
    base_path = pathlib.Path(base)
    (base_path / "artifacts/jobs").mkdir(parents=True, exist_ok=True)
    xlsx_path = base_path / "artifacts/jobs/org-roles-tracker.xlsx"
    df.to_excel(xlsx_path, index=False)
    return Store(base), base_path, xlsx_path

cols = list(COLUMN_MAP.keys())

def _norm(v):
    if v is None or v is pd.NA:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (pd.Timestamp, datetime.datetime)):
        return v.strftime("%Y-%m-%d")
    return v

def _is_number(x):
    return isinstance(x, (int, float, np.integer, np.floating))

def _same(a, b):
    a, b = _norm(a), _norm(b)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if _is_number(a) and _is_number(b):
        return float(a) == float(b)
    return str(a).strip() == str(b).strip()

# 1. load migrates
row1 = {col: None for col in cols}
row1.update({"Org": "Acme", "Title": "Engineer", "Status": "Open", "Priority": 1})
row2 = {col: None for col in cols}
row2.update({"Org": "Beta", "Title": "Analyst", "Status": "Closed", "Priority": 2})
base_df = pd.DataFrame([row1, row2])
store, base_path, xlsx_path = make_store(base_df)
loaded = store.load()
check('load_migrates_row_count', len(loaded) == len(base_df))
match = True
msg = ''
for col in cols:
    for i in range(len(base_df)):
        if not _same(base_df.at[i, col], loaded.at[i, col]):
            match = False
            msg = f"col {col} row {i} base {base_df.at[i,col]!r} loaded {loaded.at[i,col]!r}"
            break
    if not match:
        break
check('load_columns_match', match, msg)

# 2. idempotent load
store.load()
check('load_idempotent_row_count', store.count() == len(base_df))
changes = store.history()
check('load_idempotent_changes_count', len(changes) == len(base_df))

# 3. hand edit xlsx status change
mod_df = loaded.copy()
mod_df.at[0, "Status"] = "InProgress"
mod_df.to_excel(xlsx_path, index=False)
os.utime(xlsx_path, None)
store.load()
loaded = store.load()  # refresh after edit
log = store.history()
actor_entries = [c for c in log if c['actor'] == 'xlsx-edit']
check('xlsx_edit_logged', any(e['field'] == 'status' and e['old_value'] == 'Open' and e['new_value'] == 'InProgress' for e in actor_entries))

# 4. export roundtrip
export_path = store.export_xlsx()
new_base = tempfile.mkdtemp()
new_path = pathlib.Path(new_base) / "artifacts/jobs/org-roles-tracker.xlsx"
new_path.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(export_path, new_path)
new_store = Store(new_base)
new_df = new_store.load()
match2 = True
msg2 = ''
for col in cols:
    for i in range(len(loaded)):
        if not _same(loaded.at[i, col], new_df.at[i, col]):
            match2 = False
            msg2 = f"col {col} row {i} loaded {loaded.at[i,col]!r} exported {new_df.at[i,col]!r}"
            break
    if not match2:
        break
check('export_roundtrip_equal', match2, msg2)

# 5. set_field updates and logs
first_id = loaded['_id'][0]
res = store.set_field(first_id, 'status', 'Closed')
check('set_field_changed_true', res['changed'] is True)
log2 = store.history(role_id=first_id)
check('set_field_actor_dashboard', any(e['actor']=='dashboard' and e['field']=='status' for e in log2))
res2 = store.set_field(first_id, 'status', 'Closed')
check('set_field_no_change', res2['changed'] is False)
log3 = store.history(role_id=first_id)
check('set_field_no_extra_log', len([e for e in log3 if e['actor']=='dashboard' and e['field']=='status']) == 1)

# 6. unknown field and id errors
try:
    store.set_field(first_id, 'unknown_field', 'x')
    ok = False
except ValueError:
    ok = True
check('set_field_unknown_field', ok)
try:
    store.set_field('nonexistent-id', 'status', 'x')
    ok2 = False
except KeyError:
    ok2 = True
check('set_field_unknown_id', ok2)

# 7. save_df appending new row
new_row = {col: None for col in cols}
new_row.update({"Org": "Gamma", "Title": "Manager", "Status": "Open"})
append_df = pd.concat([loaded, pd.DataFrame([new_row])], ignore_index=True)
save_summary = store.save_df(append_df)
check('save_df_inserted_one', save_summary['inserted'] == 1)
post = store.to_df()
check('save_df_existing_unchanged', post.loc[0, 'Status'] == loaded.loc[0, 'Status'])

# 8. duplicate org+title rows distinct ids
dup_rows = pd.DataFrame([
    {"Org": "DupOrg", "Title": "SameTitle", "Status": "Open"},
    {"Org": "DupOrg", "Title": "SameTitle", "Status": "Closed"}
])
for col in cols:
    if col not in dup_rows.columns:
        dup_rows[col] = None
store2, _, _ = make_store(dup_rows)
store2.load()
ids = store2.to_df()['_id']
check('duplicate_ids_distinct', len(set(ids)) == 2)

# 9. blank handling
blank_df = pd.DataFrame([{col: None for col in cols}])
blank_df.at[0, 'Org'] = 'Blank'
blank_df.at[0, 'Title'] = 'Test'
store3, _, _ = make_store(blank_df)
store3.load()
bid = store3.to_df()['_id'][0]
res_blank = store3.set_field(bid, 'status', '')
check('blank_to_blank_no_change', res_blank['changed'] is False)
store3.set_field(bid, 'status', 'Active')
res_back = store3.set_field(bid, 'status', None)
check('blank_from_value_logged', res_back['changed'] is True)

# 10. numeric equivalence
num_df = pd.DataFrame([{col: None for col in cols}])
num_df.at[0, 'Org'] = 'Num'
num_df.at[0, 'Title'] = 'NumTitle'
num_df.at[0, 'Priority'] = 1
store4, _, _ = make_store(num_df)
store4.load()
id_num = store4.to_df()['_id'][0]
res_num = store4.set_field(id_num, 'priority', 1.0)
check('numeric_equivalence_no_change', res_num['changed'] is False)

if failures:
    sys.exit(1)
else:
    sys.exit(0)
