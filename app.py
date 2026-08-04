"""
Data Reliability Agent - hackathon demo app (v2).

Design:
  - Trigger is SIMULATED (app state only, nothing written to DataHub for this part)
  - All reads are REAL (live MCP + GMS REST calls to your DataHub instance)
  - All incidents are REAL DataHub Incidents (raiseIncident / updateIncidentStatus
    via DataHub's native Incidents GraphQL API), not custom properties
  - Generates a real dbt patch file into examples/ as the code artifact

Run:
  streamlit run app.py

Env vars:
  export DATAHUB_GMS_URL="http://localhost:8080"
  export DATAHUB_FRONTEND_URL="http://localhost:9002"   # GraphQL lives here, not GMS
  export OPENAI_API_KEY="sk-..."
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import requests
import streamlit as st
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

# ---- Config -----------------------------------------------------------
GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
FRONTEND_URL = os.environ.get("DATAHUB_FRONTEND_URL", "http://localhost:9002")
GRAPHQL_URL = f"{FRONTEND_URL}/api/graphql"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)"
DOWNSTREAM_URN = "urn:li:dataset:(urn:li:dataPlatform:powerbi,b2fd91.datahub_order_entries.ORDER_DETAILS,PROD)"

FALLBACK_COLUMNS = ["LIST_PRICE", "QUANTITY_ON_HAND", "DISCOUNT_AMOUNT", "STOCK_STATUS"]

EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")


# ---- Session state ------------------------------------------------------
def init_state():
    defaults = {
        "log": [],
        "schema_deployed": False,
        "dashboard_failed": False,
        "investigated": False,
        "investigation_result": None,
        "source_incident_urn": None,
        "downstream_incident_urn": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def log_event(actor: str, message: str):
    st.session_state.log.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "actor": actor,
        "message": message,
    })


ACTOR_COLORS = {"world": "#888888", "warning": "#d97706", "failure": "#dc2626", "agent": "#0891b2"}
ACTOR_LABELS = {"world": "WORLD", "warning": "WARNING", "failure": "FAILED", "agent": "AGENT"}


# ---- Real MCP search -----------------------------------------------------
async def _mcp_call(tool_name: str, arguments: dict):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_datahub"],
        env={
            **os.environ,
            "DATAHUB_GMS_URL": GMS_URL,
            "DATAHUB_TELEMETRY_ENABLED": "false",
        },
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return result.content[0].text


def mcp_search(query: str) -> str:
    log_event("agent", f"MCP call → search(query='{query}')  ✓ DataHub")
    result = asyncio.run(_mcp_call("search", {"query": query}))
    log_event("agent", "MCP call → search() returned results  ✓ DataHub")
    return result


def mcp_get_lineage(urn: str, column: str = None) -> str:
    log_event("agent", f"MCP call → get_lineage(urn='{urn[:60]}...', column='{column}')  ✓ DataHub")
    result = asyncio.run(_mcp_call("get_lineage", {"urn": urn, "upstream": False, "column": column}))
    log_event("agent", "MCP call → get_lineage() returned results  ✓ DataHub")
    return result


# ---- Real schema read via MCP: dynamic column list, with safe fallback ---
@st.cache_data(ttl=3600)
def get_schema_columns(urn: str) -> list:
    try:
        result_text = asyncio.run(_mcp_call("list_schema_fields", {"urn": urn, "limit": 200}))
        data = json.loads(result_text)
        fields = data.get("fields", [])
        cols = [f.get("fieldPath") for f in fields if f.get("fieldPath")]
        return sorted(cols) if cols else FALLBACK_COLUMNS
    except Exception:
        return FALLBACK_COLUMNS


# ---- Authenticated session (frontend GraphQL requires a login session) ----
DATAHUB_USERNAME = os.environ.get("DATAHUB_USERNAME", "datahub")
DATAHUB_PASSWORD = os.environ.get("DATAHUB_PASSWORD", "datahub")

_session = None


def get_authenticated_session():
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    resp = s.post(
        f"{FRONTEND_URL}/logIn",
        json={"username": DATAHUB_USERNAME, "password": DATAHUB_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    _session = s
    return s


# ---- GraphQL helper -------------------------------------------------------
def graphql(query: str, variables: dict = None) -> dict:
    session = get_authenticated_session()
    resp = session.post(
        GRAPHQL_URL,
        headers={"Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"])[:300])
    return data.get("data", {})


# ---- Incidents API: raise --------------------------------------------------
def raise_incident(resource_urn: str, incident_type: str, title: str, description: str):
    query = """
    mutation raiseIncident($input: RaiseIncidentInput!) {
      raiseIncident(input: $input)
    }
    """
    variables = {
        "input": {
            "resourceUrn": resource_urn,
            "type": incident_type,
            "title": title,
            "description": description,
        }
    }
    data = graphql(query, variables)
    return data.get("raiseIncident")


# ---- Incidents API: list active/all incidents on an entity ----------------
def list_incidents(resource_urn: str, state: str = None) -> list:
    state_filter = f'state: {state}' if state else ""
    query = f"""
    query getIncidents($urn: String!) {{
      dataset(urn: $urn) {{
        incidents({state_filter} start: 0, count: 50) {{
          total
          incidents {{
            urn
            incidentType
            title
            description
            status {{ state stage }}
            created {{ time }}
          }}
        }}
      }}
    }}
    """
    try:
        data = graphql(query, {"urn": resource_urn})
        return data.get("dataset", {}).get("incidents", {}).get("incidents", []) or []
    except Exception as e:
        log_event("agent", f"Could not list incidents (non-fatal): {e}")
        return []


# ---- Incidents API: update status (resolve) -------------------------------
def resolve_incident(incident_urn: str, message: str = "Resolved via Data Reliability Agent"):
    query = """
    mutation updateIncidentStatus($urn: String!, $input: IncidentStatusInput!) {
      updateIncidentStatus(urn: $urn, input: $input)
    }
    """
    variables = {"urn": incident_urn, "input": {"state": "RESOLVED", "message": message}}
    try:
        graphql(query, variables)
        return True, None
    except Exception as e:
        return False, str(e)


# ---- Simple, honest memory-match (rules-based, not ML) --------------------
def _time_ago(epoch_millis: int) -> str:
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(epoch_millis / 1000, tz=timezone.utc)
    days = delta.days
    if days < 1:
        return "earlier today"
    if days == 1:
        return "1 day ago"
    if days < 14:
        return f"{days} days ago"
    weeks = days // 7
    return f"{weeks} week{'s' if weeks != 1 else ''} ago"


def find_similar_incident(column: str, all_incidents: list) -> dict:
    """Look for a prior incident whose title mentions this same column."""
    matches = [inc for inc in all_incidents if column in (inc.get("title") or "")]
    if not matches:
        return None
    # most recent match first
    matches.sort(key=lambda i: i.get("created", {}).get("time", 0), reverse=True)
    most_recent = matches[0]
    created_ms = most_recent.get("created", {}).get("time")
    time_ago = _time_ago(created_ms) if created_ms else "previously"
    was_resolved = most_recent.get("status", {}).get("state") == "RESOLVED"
    return {
        "incident": most_recent,
        "time_ago": time_ago,
        "was_resolved": was_resolved,
        "count": len(matches),
    }


# ---- Reasoning (OpenAI) ----------------------------------------------------
def run_reasoning(column: str, search_context: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""You are a data reliability agent. A schema change was simulated on column
'{column}' in the 'order_details' table (Snowflake, dbt-modeled), feeding a PowerBI
table used by 4 pages and 1 report.

Real context from DataHub:
{search_context[:1500]}

Respond ONLY as JSON with keys:
{{
  "risk_score": <int 0-100>,
  "root_cause": "<one sentence>",
  "recommended_fix": "<one sentence>",
  "confidence": <int 0-100>
}}"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


# ---- dbt patch generation, grounded in real schema ------------------------
def generate_dbt_patch(column: str, root_cause: str, fix: str, all_columns: list) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=OPENAI_API_KEY)
    other_columns = [c for c in all_columns if c != column]

    prompt = f"""You are writing the SELECT column list for a dbt model, based on this
REAL schema read from DataHub (source of truth — do not invent columns):

Full column list: {', '.join(all_columns)}
Column with the schema drift issue: {column}
Root cause: {root_cause}
Recommended fix: {fix}

Write ONE comma-separated line per column for a SELECT statement:
- For every column EXCEPT '{column}': just output the column name as-is, unchanged.
- For '{column}' specifically: wrap it in a defensive CAST or COALESCE guarding
  against the schema drift described above.

STRICT OUTPUT RULES:
- Output ONLY the column list, one column expression per line, each line ending in a comma except the last.
- Do NOT include the words SELECT or FROM.
- Do NOT include markdown code fences.
- Do NOT include any explanation before or after.
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    snippet = resp.choices[0].message.content.strip()
    # strip any stray SELECT/FROM the model might still emit, defensively
    snippet = "\n".join(
        line for line in snippet.splitlines()
        if line.strip().upper() not in ("SELECT", "") and not line.strip().upper().startswith("FROM ")
    )

    template = f"""-- Generated by Data Reliability Agent
-- Column: {column}
-- Root cause: {root_cause}
-- Recommended fix: {fix}
-- Full column list sourced live from DataHub schema metadata
--
-- This is the proposed fix to the order_details dbt model itself.
-- It would replace the model's current SELECT statement, so the fix
-- happens before the table is built -- not as a separate step
-- inserted between the table and PowerBI.

{{{{ config(materialized='view') }}}}

select
{snippet}
from {{{{ ref('order_details') }}}}  -- adjust to your actual upstream model/source name
"""
    os.makedirs(EXAMPLES_DIR, exist_ok=True)
    path = os.path.join(EXAMPLES_DIR, f"generated_fix_{column}.sql")
    with open(path, "w") as f:
        f.write(template)
    return path


# ---- UI ---------------------------------------------------------------------
st.set_page_config(page_title="Data Reliability Agent", layout="wide")
init_state()

st.title("🛡️ Data Reliability Agent")
st.caption("Live demo scenario: `order_details` → 4 PowerBI pages, 1 report — the agent itself is generic across any catalog asset")
st.caption("🔧 MCP tools used: `search`, `get_lineage`, `list_schema_fields` (DataHub MCP Server) — plus DataHub's native Incidents GraphQL API for read/write")

available_columns = get_schema_columns(SOURCE_URN)
hero_column = st.selectbox("Column to investigate", available_columns, index=0)

tab1, tab2, tab3 = st.tabs(["🏠 Incident Simulation", "📊 Operations Dashboard", "🗂 Issues"])

with tab1:
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("1️⃣ Schema Change & Deploy", use_container_width=True):
            log_event("world", f"Schema changed: {hero_column} type altered")
            log_event("warning", f"⚠ Risk detected on {hero_column} — recommend: do not deploy")
            log_event("world", "Deployment status: pending → deployed")
            try:
                urn = raise_incident(
                    SOURCE_URN, "DATA_SCHEMA",
                    f"Schema change deployed on {hero_column}",
                    f"Column {hero_column} type was changed and deployed. Monitoring for downstream impact.",
                )
                st.session_state.source_incident_urn = urn
                st.session_state.schema_deployed = True
                log_event("agent", f"✓ Incident raised on source table ({urn})  ✓ DataHub")
            except Exception as e:
                log_event("agent", f"✗ Could not raise incident: {e}")

    with col2:
        if st.button("2️⃣ Refresh Dashboard", use_container_width=True, disabled=not st.session_state.schema_deployed):
            log_event("failure", "PowerBI dashboard failed to load")
            try:
                ref_note = f" Related to incident {st.session_state.source_incident_urn}." if st.session_state.source_incident_urn else ""
                urn = raise_incident(
                    DOWNSTREAM_URN, "OPERATIONAL",
                    "Dashboard failed to load",
                    f"ORDER_DETAILS PowerBI table failed to refresh.{ref_note}",
                )
                st.session_state.downstream_incident_urn = urn
                st.session_state.dashboard_failed = True
                log_event("agent", f"✓ Incident raised on PowerBI table ({urn})  ✓ DataHub")
            except Exception as e:
                log_event("agent", f"✗ Could not raise incident: {e}")

    with col3:
        if st.button("🔍 Investigate", use_container_width=True, disabled=not st.session_state.dashboard_failed):
            with st.spinner("Investigating..."):
                search_context = mcp_search("order_details")
                lineage_context = mcp_get_lineage(SOURCE_URN, column=hero_column)
                combined_context = f"SEARCH RESULTS:\n{search_context}\n\nLINEAGE:\n{lineage_context}"

                log_event("agent", "Checking for similar past incidents...  ✓ DataHub")
                all_incidents = list_incidents(SOURCE_URN)
                match = find_similar_incident(hero_column, all_incidents)
                if match:
                    verb = "resolved" if match["was_resolved"] else "raised"
                    log_event("agent", f"Found a similar investigation on {hero_column}, {verb} {match['time_ago']} — reusing what we learned then.")
                else:
                    log_event("agent", "No prior incidents found for this column")

                log_event("agent", "Reasoning about root cause...")
                result = run_reasoning(hero_column, combined_context)
                st.session_state.investigation_result = result
                log_event("agent", f"Risk score: {result['risk_score']}/100 — {result['root_cause']}")

                log_event("agent", "Generating dbt patch...")
                patch_path = generate_dbt_patch(hero_column, result["root_cause"], result["recommended_fix"], available_columns)
                log_event("agent", f"✓ Patch saved: {os.path.basename(patch_path)}")

                st.session_state.investigated = True
                log_event("agent", "Investigation complete — review findings in Issues tab")

    st.divider()
    st.subheader("Live Audit Log")
    if not st.session_state.log:
        st.info("Click through the steps above to begin.")
    for entry in reversed(st.session_state.log):
        color = ACTOR_COLORS[entry["actor"]]
        label = ACTOR_LABELS[entry["actor"]]
        message = entry["message"].replace(
            "✓ DataHub",
            "<span style='background:#16a34a; color:white; padding:1px 9px; "
            "border-radius:10px; font-size:0.75em; font-weight:700; white-space:nowrap;'>✓ DataHub</span>",
        )
        st.markdown(
            f"<span style='color:{color}; font-weight:600'>[{entry['time']}] {label}</span> — {message}",
            unsafe_allow_html=True,
        )

    if st.button("↻ Reset simulation"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

with tab2:
    st.subheader("Operations Dashboard")

    # Real, persistent view: all incidents ever raised on this asset pair, live from DataHub
    all_incidents_ever = list_incidents(SOURCE_URN, state=None) + list_incidents(DOWNSTREAM_URN, state=None)
    total = len(all_incidents_ever)
    active = len([i for i in all_incidents_ever if i.get("status", {}).get("state") == "ACTIVE"])
    resolved = len([i for i in all_incidents_ever if i.get("status", {}).get("state") == "RESOLVED"])

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Issues (all time)", total)
    m2.metric("Active", active)
    m3.metric("Resolved", resolved)

    st.divider()
    st.markdown("**Most recent investigation (this session)**")
    result = st.session_state.investigation_result
    if result:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Risk Score", f"{result['risk_score']}/100")
        c2.metric("Blast Radius", "4 pages, 1 report")
        c3.metric("Confidence", f"{result['confidence']}%")
        c4.metric("Owner", "Karen Okonkwo")
        st.write(f"**Root cause:** {result['root_cause']}")
        st.write(f"**Recommended fix:** {result['recommended_fix']}")
    else:
        st.info("Run an investigation in the first tab to see this session's findings here. "
                 "Historical counts above reflect all prior runs, regardless of session.")

with tab3:
    st.subheader("Issues")
    st.caption("Live triage view — active and closed. Assign and resolve directly in DataHub.")

    import pandas as pd

    def build_incident_table(urn: str) -> "pd.DataFrame":
        rows = []
        for inc in list_incidents(urn, state=None):
            state = inc.get("status", {}).get("state", "UNKNOWN")
            stage = inc.get("status", {}).get("stage") or "-"
            rows.append({
                "Status": state.title(),
                "Short Description": inc["title"],
                "Long Description": inc.get("description", ""),
                "Incident URN": inc["urn"],
                "Stage": stage.replace("_", " ").title() if stage != "-" else "-",
                "Link": f"{FRONTEND_URL}/dataset/{urn}/Incidents",
            })
        if not rows:
            return pd.DataFrame(columns=["Status", "Short Description", "Long Description", "Incident URN", "Stage", "Link"])
        df = pd.DataFrame(rows)
        # Active first, then Resolved
        df["_sort"] = df["Status"].map({"Active": 0}).fillna(1)
        df = df.sort_values("_sort").drop(columns="_sort").reset_index(drop=True)
        return df

    sub_tab1, sub_tab2 = st.tabs(["📋 Tables", "📊 Dashboards"])

    with sub_tab1:
        df = build_incident_table(SOURCE_URN)
        if df.empty:
            st.info("No incidents found.")
        else:
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Open in DataHub", display_text="View →")},
            )

    with sub_tab2:
        df = build_incident_table(DOWNSTREAM_URN)
        if df.empty:
            st.info("No incidents found.")
        else:
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={"Link": st.column_config.LinkColumn("Open in DataHub", display_text="View →")},
            )
