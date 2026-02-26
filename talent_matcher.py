# talent_matcher_v1.py
import re
import pandas as pd
import streamlit as st

# ----------------------------
# Page config
# ----------------------------
st.set_page_config(page_title="Talent Matcher — Phase 1", layout="wide")
st.title("Talent Matcher")

# ----------------------------
# Constants / Column map
# ----------------------------
COL_MAP = {
    "candidate_id": ["Applicant ID"],
    "name": ["Applicant Name"],
    "title": ["Job Title"],
    "years_exp": ["Experience"],
    "skills": ["Skills"],
    "city": ["City"],
    "state": ["State"],
    "zip": ["Zip Code"],
    "email": ["Email Address"],
    "mobile": ["Mobile Number"],
    "work_auth": ["Work Authorization"],
    "tag": ["Tag"],
    # Optional boolean domain flags (set any of these columns to truthy indicators like Y/Yes/1/True)
    "domains": ["MMIS","EDI","ICD","IVV","WIC","E&E","SNAP","TANF","FACETS","Welfare","EBT","MITA","CHIP","X12"]
}

# ----------------------------
# Helpers for column finding / parsing
# ----------------------------
def _norm(s):
    return str(s).strip().lower()

def find_col(df_cols, candidates):
    """Return the real Excel column that matches any name in `candidates` (case/space/& tolerant)."""
    norm_df_cols = {_norm(c): c for c in df_cols}
    # exact
    for c in candidates:
        key = _norm(c)
        if key in norm_df_cols:
            return norm_df_cols[key]
    # tolerant: & vs and, double spaces
    for c in candidates:
        key = _norm(c).replace("&", "and").replace("  ", " ")
        for k in norm_df_cols:
            if key == k.replace("&", "and").replace("  ", " "):
                return norm_df_cols[k]
    return None

def skills_to_set(x):
    """Parse a messy skills cell into a set of lowercase tokens."""
    if pd.isna(x):
        return set()
    tokens = re.split(r"[;,/|]", str(x))  # split on , ; / |
    return set(t.strip().lower() for t in tokens if t.strip())

def load_candidates_from_excel(file_or_path) -> pd.DataFrame:
    """Read Excel → standardized DataFrame with consistent column names."""
    df_raw = pd.read_excel(file_or_path, sheet_name=0, engine="openpyxl")

    out = pd.DataFrame()
    # map standard columns
    for out_name, excel_names in COL_MAP.items():
        if out_name == "domains":
            continue  # handled below
        src = find_col(df_raw.columns, excel_names)
        out[out_name] = df_raw[src] if src is not None else None

    # Keep city/state/zip as separate fields and also build a combined location for display convenience
    out["city"] = out.get("city")
    out["state"] = out.get("state")
    out["zip"] = out.get("zip")
    out["location"] = (
        out[["city","state","zip"]].astype(str).replace("nan","")
        .agg(lambda r: ", ".join([x for x in r if x and x.lower()!="nan"]).strip(", "), axis=1)
    )

    # types / parsing
    out["years_exp"] = pd.to_numeric(out["years_exp"], errors="coerce").fillna(0)
    out["skills_set"] = out["skills"].apply(skills_to_set)

    # optional boolean domain flags
    for d in COL_MAP["domains"]:
        col = find_col(df_raw.columns, [d])
        series = df_raw[col] if col is not None else False
        out[d] = (
            pd.Series(series)
            .fillna(False)
            .astype(str)
            .str.strip()
            .str.lower()
            .isin(["1","y","yes","true","t","x","✓","✔"])
        )

    # tidy column order
    order = [
        "candidate_id","name","title","years_exp",
        "city","state","zip","location",
        "skills","email","mobile","work_auth","tag"
    ] + COL_MAP["domains"]
    keep = [c for c in order if c in out.columns]
    return out[keep]

# ----------------------------
# Skill boolean expression parsing (AND/OR/parentheses)
# ----------------------------
TOK_AND = "AND"
TOK_OR = "OR"
TOK_LP = "("
TOK_RP = ")"

def _tokenize_expr(expr: str):
    if not expr or not expr.strip():
        return []
    # Replace commas with OR for convenience
    expr = expr.replace(",", " OR ")
    # Normalize spacing around parentheses
    expr = re.sub(r"(\()", r" ( ", expr)
    expr = re.sub(r"(\))", r" ) ", expr)
    raw = expr.strip().split()
    tokens = []
    for t in raw:
        up = t.upper()
        if up in (TOK_AND, TOK_OR):
            tokens.append(up)
        elif t == "(" or t == ")":
            tokens.append(t)
        else:
            tokens.append(t)  # skill literal
    return tokens

def _to_rpn(tokens):
    """Shunting-yard: convert infix tokens to Reverse Polish Notation."""
    prec = {TOK_AND: 2, TOK_OR: 1}
    out = []
    op = []
    for t in tokens:
        if t == TOK_LP:
            op.append(t)
        elif t == TOK_RP:
            while op and op[-1] != TOK_LP:
                out.append(op.pop())
            if op and op[-1] == TOK_LP:
                op.pop()
        elif t in (TOK_AND, TOK_OR):
            while op and op[-1] in prec and prec[op[-1]] >= prec[t]:
                out.append(op.pop())
            op.append(t)
        else:
            out.append(t)  # skill literal
    while op:
        out.append(op.pop())
    return out

# Unified literal check (exact token OR word-boundary substring)
_WORD_RE_CACHE = {}

def _word_in_text(lit: str, text: str) -> bool:
    """Word-boundary match: 'lit' appears as a whole word in 'text'."""
    if not lit or not text:
        return False
    lit = lit.strip().lower()
    pat = _WORD_RE_CACHE.get(lit)
    if pat is None:
        pat = re.compile(rf"\b{re.escape(lit)}\b", flags=re.IGNORECASE)
        _WORD_RE_CACHE[lit] = pat
    return bool(pat.search(text))

def _row_has_literal(row, literal: str, use_substring: bool) -> bool:
    """True if the row has the literal skill (exact token OR word-boundary substring)."""
    s = literal.strip().lower()
    skills_set = row.get("skills_set", set()) or set()
    if s in skills_set:
        return True
    if use_substring:
        return _word_in_text(s, str(row.get("skills", "")).lower())
    return False

def eval_skill_expr_on_row(row, rpn_tokens, use_substring=True) -> bool:
    """Evaluate boolean RPN against a row's skills using unified literal check."""
    if not rpn_tokens:
        return True
    stack = []
    for t in rpn_tokens:
        if t in (TOK_AND, TOK_OR):
            if len(stack) < 2:
                return False
            b = stack.pop()
            a = stack.pop()
            stack.append((a and b) if t == TOK_AND else (a or b))
        else:
            stack.append(_row_has_literal(row, t, use_substring))
    return bool(stack[-1]) if stack else False

# ----------------------------
# Sidebar — Data section
# ----------------------------
st.sidebar.header("Data")

# Optional default path controls (so the code never references undefined names)
use_default = st.sidebar.checkbox("Use default Excel path", value=False)
DEFAULT_XLSX_PATH = st.sidebar.text_input(
    "Default Excel path (optional)", value="",
    help="Provide an absolute path on your machine if you prefer a default file."
)

uploaded = st.sidebar.file_uploader("Upload Excel (.xlsx)", type=["xlsx"])

# ----------------------------
# Sidebar — Skill logic & filters
# ----------------------------
st.sidebar.header("Skill Logic (Boolean)")
skills_expr = st.sidebar.text_input(
    "Enter skills boolean expression",
    value="python AND sql AND tableau",
    help="Use AND/OR and parentheses. Example: (python AND sql) OR tableau. Commas work like OR."
)
use_substring = st.sidebar.checkbox("Allow substring match in free-text skills", value=True)

st.sidebar.header("Experience")
min_years = st.sidebar.number_input("Minimum years of experience (≥)", 0.0, 50.0, 2.0, 0.5)

st.sidebar.header("Filters")
title_contains = st.sidebar.text_input("Title contains", "")
city_contains = st.sidebar.text_input("City contains", "")
state_contains = st.sidebar.text_input("State contains", "")
work_auth_contains = st.sidebar.text_input("Work authorization contains", "")
domain_filter = st.sidebar.multiselect("Domain tags (all must be True)", COL_MAP["domains"])

# Optional score threshold (applies to match_score)
min_score = st.sidebar.slider("Minimum match score", 0, 100, 50, 1)

# ----------------------------
# Load data
# ----------------------------
if use_default and DEFAULT_XLSX_PATH.strip():
    try:
        df = load_candidates_from_excel(DEFAULT_XLSX_PATH.strip())
    except Exception as e:
        st.error(f"Failed to load default path file: {e}")
        st.stop()
elif uploaded is not None:
    try:
        df = load_candidates_from_excel(uploaded)
    except Exception as e:
        st.error(f"Failed to load uploaded file: {e}")
        st.stop()
else:
    st.info("Upload your Excel file (or enable 'Use default Excel path' and provide a valid path).")
    st.stop()

# ----------------------------
# Prepare tokens / literals
# ----------------------------
tokens = _tokenize_expr(skills_expr)
rpn = _to_rpn(tokens)
skill_literals = [t.strip() for t in tokens if t not in (TOK_AND, TOK_OR, TOK_LP, TOK_RP)]

# Ensure skills_set exists
if "skills_set" not in df.columns and "skills" in df.columns:
    df["skills_set"] = df["skills"].apply(skills_to_set)
elif "skills_set" not in df.columns:
    df["skills_set"] = [set()]*len(df)

# ----------------------------
# Compute skill_coverage and match_score (NO years_factor)
# coverage = (# literals satisfied via unified match) / (total literals)
# match_score = coverage * 100
# ----------------------------
def compute_coverage(row, literals, use_substring=True) -> float:
    total = len(literals)
    if total == 0:
        return 0.0
    hits = sum(1 for lit in literals if _row_has_literal(row, lit, use_substring))
    return hits / total

if len(df):
    df["skill_coverage"] = df.apply(lambda r: round(compute_coverage(r, skill_literals, use_substring=use_substring), 2), axis=1)
    df["match_score"] = (df["skill_coverage"] * 100).round(0).astype(int)
else:
    df["skill_coverage"] = []
    df["match_score"] = []

# ----------------------------
# Filtering
# ----------------------------
scored = df.copy()
mask = pd.Series(True, index=scored.index)

# Experience
mask &= scored["years_exp"] >= float(min_years)

# Title contains
if title_contains:
    mask &= scored.get("title", pd.Series("", index=scored.index)).fillna("").str.contains(title_contains, case=False)

# City/state contains
if city_contains:
    mask &= scored.get("city", pd.Series("", index=scored.index)).fillna("").str.contains(city_contains, case=False)
if state_contains:
    mask &= scored.get("state", pd.Series("", index=scored.index)).fillna("").str.contains(state_contains, case=False)

# Work authorization contains
if work_auth_contains:
    mask &= scored.get("work_auth", pd.Series("", index=scored.index)).fillna("").str.contains(work_auth_contains, case=False)

# Domain booleans (must all be True)
for d in domain_filter:
    if d in scored.columns:
        mask &= scored[d] == True

# Skills boolean expression (for pass/fail)
if rpn:
    skill_pass = scored.apply(lambda r: eval_skill_expr_on_row(r, rpn, use_substring=use_substring), axis=1)
    mask &= skill_pass

# Minimum match score
mask &= scored["match_score"] >= float(min_score)

# Apply mask and sort
result = scored.loc[mask].copy()
if not result.empty:
    result = result.sort_values(["match_score","years_exp"], ascending=[False, False])

# ----------------------------
# KPIs (updated requirement)
# ----------------------------
k1, k2 = st.columns(2)
k1.metric("Candidates found", len(result))
avg_years_filtered = result["years_exp"].mean() if not result.empty else 0.0
k2.metric("Avg years (filtered)", f"{avg_years_filtered:.1f}")

# ----------------------------
# Table
# ----------------------------
st.subheader("Ranked Candidates")
show_cols = [
    "candidate_id","name","title","years_exp",
    "city","state","zip","location",
    "skills","match_score","skill_coverage",
    "work_auth","email","mobile","tag"
] + COL_MAP["domains"]
show_cols = [c for c in show_cols if c in result.columns]
st.dataframe(result[show_cols], use_container_width=True, height=520)

# ----------------------------
# Download
# ----------------------------
csv = result[show_cols].to_csv(index=False).encode("utf-8")
st.download_button("Download shortlist (CSV)", csv, "shortlist.csv", "text/csv")

st.caption("Use AND/OR/() in Skills; City/State filters; and the score slider to refine your shortlist.")
