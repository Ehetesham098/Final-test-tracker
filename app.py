"""
Final Test Tracking App
========================
Streamlit app that connects directly to the MES SQL Server database
and shows:

  Page 1 - Summary:        Station-by-station yield/pareto for a WO/device
  Page 2 - Device Tracker:  All devices currently in Wait/Run at Final test,
                             along with their latest TESTRESULT_800G_MASTER row

Run with:
    streamlit run app.py

Requires:
    pip install streamlit pandas pyodbc
    (and the "ODBC Driver 17 (or 18) for SQL Server" installed on the machine)
"""

import streamlit as st
import pandas as pd
import pyodbc
import time
from datetime import date


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Final Test Tracker",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def build_conn_str(server: str, database: str, username: str, password: str,
                   use_windows_auth: bool, driver: str) -> str:
    if use_windows_auth:
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"Trusted_Connection=yes;"
            f"MARS_Connection=yes;"
        )
    else:
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={password};"
            f"MARS_Connection=yes;"
        )


def run_query(conn_str: str, sql: str, params: tuple = (),
              retries: int = 3) -> pd.DataFrame:
    last_err = None
    for attempt in range(retries):
        try:
            conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
            df = pd.read_sql(sql, conn, params=params)
            conn.close()
            return df
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise last_err


# ---------------------------------------------------------------------------
# SQL templates
# ---------------------------------------------------------------------------
SUMMARY_SQL = """
WITH StationSeq AS (
    SELECT *
    FROM (VALUES
        (1,  'StartRuncard'),
        (2,  'TRX_FW_Writing'),
        (3,  'TRX_DDMI_Cal'),
        (4,  'TRX_TP2_TP3_RT_Test'),
        (5,  'TRX_TP2_TP3_LT_Test'),
        (6,  'TRX_TP2_TP3_HT_Test'),
        (7,  'TRX_Burn-in_Test'),
        (8,  'TRX_3T_BER_Symbol_Error'),
        (9,  'TRX_BER_Symbol_Error'),
        (10, 'TRX_Mode_Hopping'),
        (11, 'Final test'),
        (12, 'TRX_Switch_Test'),
        (13, 'TRX_Test_OQC'),
        (14, 'INVT_PCS')
    ) AS S(Seq, OPERATION)
),
WOList AS (
    SELECT WO
    FROM v_MasterWOInfo
    WHERE DEVICE = ?
      AND TRY_CONVERT(date, STARTDATE, 111) >= ?
),
RunCardAgg AS (
    SELECT
        OPERATION,
        SUM(CASE WHEN STATUS = 'Wait' THEN QUANTITY ELSE 0 END) AS Wait,
        SUM(CASE WHEN STATUS = 'Run'  THEN QUANTITY ELSE 0 END) AS [Run]
    FROM V_MasterRuncardInfo
    WHERE WO IN (SELECT WO FROM WOList)
      AND STATUS != 'Terminated'
    GROUP BY OPERATION
),
LatestRows AS (
    SELECT
        M.*,
        ROW_NUMBER() OVER (
            PARTITION BY M.COMPONENTID, M.OPERATION
            ORDER BY M.LIV_MASTER_SID DESC
        ) AS rn
    FROM [MES].[dbo].[TESTRESULT_800G_MASTER] M
    WHERE M.WO IN (SELECT WO FROM WOList)
),
TestAgg AS (
    SELECT
        OPERATION,
        SUM(CASE WHEN UPPER(LTRIM(RTRIM(TESTRESULT))) = 'PASS' THEN 1 ELSE 0 END) AS PassQty,
        SUM(CASE WHEN UPPER(LTRIM(RTRIM(TESTRESULT))) = 'FAIL' THEN 1 ELSE 0 END) AS FailQty
    FROM LatestRows
    WHERE rn = 1
    GROUP BY OPERATION
)
SELECT
    S.OPERATION,
    ISNULL(R.Wait, 0) + ISNULL(R.[Run], 0) + ISNULL(T.PassQty, 0) AS InputQty,
    ISNULL(R.Wait, 0) AS Wait,
    ISNULL(R.[Run], 0) AS [Run],
    ISNULL(T.PassQty, 0) AS PassQty,
    ISNULL(T.FailQty, 0) AS FailQty,
    CASE
        WHEN ISNULL(T.PassQty, 0) + ISNULL(T.FailQty, 0) = 0 THEN 0.00
        ELSE CAST(
            ISNULL(T.PassQty, 0) * 100.0
            / (ISNULL(T.PassQty, 0) + ISNULL(T.FailQty, 0))
            AS decimal(10,2)
        )
    END AS YieldPercent
FROM StationSeq S
LEFT JOIN RunCardAgg R ON S.OPERATION = R.OPERATION
LEFT JOIN TestAgg T ON S.OPERATION = T.OPERATION
ORDER BY S.Seq;
"""


WAIT_RUN_SQL = """
WITH WOList AS (
    SELECT WO
    FROM v_MasterWOInfo
    WHERE DEVICE = ?
      AND TRY_CONVERT(date, STARTDATE, 111) >= ?
),
RunCardDetail AS (
    SELECT
        OPERATION,
        STATUS,
        LOTID,
        QUANTITY
    FROM V_MasterRuncardInfo
    WHERE WO IN (SELECT WO FROM WOList)
      AND OPERATION = 'Final test'
      AND STATUS IN ('Wait','Run')
      AND STATUS != 'Terminated'
)
SELECT
    R.OPERATION,
    R.STATUS,
    R.LOTID,
    R.QUANTITY,
    T.COMPONENTID
FROM RunCardDetail R
CROSS APPLY (
    SELECT DISTINCT COMPONENTID
    FROM [MES].[dbo].[TESTRESULT_800G_MASTER]
    WHERE LOT = R.LOTID
) T
ORDER BY R.STATUS, R.LOTID, T.COMPONENTID;
"""


# Raw rows from TRX_TEST for a COMPONENTID ordered by TESTNUMBER DESC
DEVICE_DETAIL_SQL = """
SELECT *
FROM [MES].[dbo].[TestResult_800G_2XFR4_TRX_TEST]
WHERE COMPONENTID = ?
ORDER BY TESTNUMBER DESC
"""


# Batch summary (one row per COMPONENTID) for merging into the wait/run table
def device_summary_sql(component_ids: list[str]) -> tuple[str, list]:
    placeholders = ",".join(["?"] * len(component_ids))
    sql = f"""
    WITH MasterRanked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY COMPONENTID
                ORDER BY LIV_MASTER_SID DESC
            ) AS rn
        FROM [MES].[dbo].[TESTRESULT_800G_MASTER]
        WHERE COMPONENTID IN ({placeholders})
          AND OPERATION = 'Final test'
    ),
    TrxRanked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY COMPONENTID, CHNumber
                ORDER BY TESTNUMBER DESC
            ) AS rn
        FROM [MES].[dbo].[TestResult_800G_2XFR4_TRX_TEST]
        WHERE COMPONENTID IN ({placeholders})
    ),
    TrxFails AS (
        SELECT
            COMPONENTID,
            CHNumber,
            FailureCodeID
        FROM TrxRanked
        WHERE rn = 1
          AND UPPER(LTRIM(RTRIM(CH_Pass_Fail))) = 'FAIL'
    ),
    TrxFailsAgg AS (
        SELECT
            COMPONENTID,
            STRING_AGG(CHNumber, ' | ') WITHIN GROUP (ORDER BY CHNumber) AS FailChannels,
            STRING_AGG(
                CHNumber + ': ' + ISNULL(NULLIF(LTRIM(RTRIM(FailureCodeID)),'0'), 'N/A'),
                ' | '
            ) WITHIN GROUP (ORDER BY CHNumber) AS FailureCodes
        FROM TrxFails
        GROUP BY COMPONENTID
    ),
    RunCounts AS (
        SELECT
            COMPONENTID,
            COUNT(DISTINCT TESTNUMBER) AS TimesTested
        FROM [MES].[dbo].[TestResult_800G_2XFR4_TRX_TEST]
        WHERE COMPONENTID IN ({placeholders})
          AND CHNumber LIKE '%ATS_RT%'
        GROUP BY COMPONENTID
    )
    SELECT
        M.COMPONENTID,
        M.EQUPMENT,
        M.TESTRESULT AS Result,
        M.USERID,
        M.CREATETIME AS RunTime,
        F.FailureCodes,
        ISNULL(R.TimesTested, 0) AS TimesTested
    FROM MasterRanked M
    LEFT JOIN TrxFailsAgg F ON F.COMPONENTID = M.COMPONENTID
    LEFT JOIN RunCounts R ON R.COMPONENTID = M.COMPONENTID
    WHERE M.rn = 1
    """
    return sql, component_ids + component_ids + component_ids


# ---------------------------------------------------------------------------
# Sidebar - connection + filters
# ---------------------------------------------------------------------------
st.sidebar.header("Connection")

server = st.sidebar.text_input("SQL Server", value="US_SQL01.USPL.HOME")
database = st.sidebar.text_input("Database", value="MES")
use_windows_auth = st.sidebar.checkbox("Use Windows Authentication", value=False)

username = ""
password = ""
if not use_windows_auth:
    username = st.sidebar.text_input("Username", value="omdpm")
    password = st.sidebar.text_input("Password", value="omdpm", type="password")

driver = st.sidebar.selectbox(
    "ODBC Driver",
    options=["SQL Server", "ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"],
    index=0,
)

st.sidebar.markdown("---")
st.sidebar.header("Filters")

device = st.sidebar.text_input("DEVICE", value="400454000035")
start_date = st.sidebar.date_input("Start date (>=)", value=date(2026, 5, 3))

connect_btn = st.sidebar.button("Connect / Refresh", type="primary")

st.sidebar.markdown("---")
with st.sidebar.expander("Find a column name"):
    col_search = st.text_input("Search term (e.g. user, operator)", value="user")
    if st.button("Search columns"):
        if st.session_state.get("conn_str") is not None:
            try:
                df_cols = run_query(
                    st.session_state.conn_str,
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME IN
                          ('TESTRESULT_800G_MASTER', 'TestResult_800G_2XFR4_TRX_TEST')
                      AND COLUMN_NAME LIKE ?
                    ORDER BY TABLE_NAME, COLUMN_NAME
                    """,
                    (f"%{col_search}%",),
                )
                st.dataframe(df_cols, use_container_width=True, hide_index=True)
            except Exception as e:
                st.error(f"Search failed: {e}")
        else:
            st.warning("Connect first.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("Final Test Tracking")

if "conn_str" not in st.session_state:
    st.session_state.conn_str = None

# Auto-connect on first load using defaults
if st.session_state.conn_str is None and not connect_btn and server and database:
    try:
        cs = build_conn_str(server, database, username, password, use_windows_auth, driver)
        # quick test
        test_conn = pyodbc.connect(cs, autocommit=True, timeout=10)
        test_conn.close()
        st.session_state.conn_str = cs
        st.sidebar.success("Auto-connected.")
    except Exception as e:
        st.sidebar.error(f"Auto-connect failed: {e}")

if connect_btn:
    if not server or not database:
        st.sidebar.error("Please provide a SQL Server and database name.")
    else:
        try:
            cs = build_conn_str(server, database, username, password, use_windows_auth, driver)
            test_conn = pyodbc.connect(cs, autocommit=True, timeout=10)
            test_conn.close()
            st.session_state.conn_str = cs
            st.sidebar.success("Connected.")
        except Exception as e:
            st.sidebar.error(f"Connection failed: {e}")
            st.session_state.conn_str = None

conn_str = st.session_state.conn_str

if conn_str is None:
    st.info("Enter your SQL Server connection details in the sidebar and click "
            "**Connect / Refresh** to load data.")
    st.stop()


tab_summary, tab_devices, tab_station = st.tabs([
    "Summary", "Device Tracker (Wait / Run)", "Station Daily Report"
])


# ---------------------------------------------------------------------------
# Page 1: Summary
# ---------------------------------------------------------------------------
with tab_summary:
    st.subheader(f"Station Yield Summary — DEVICE {device}, WO start >= {start_date}")

    try:
        with st.spinner("Running summary query..."):
            df_summary = run_query(
                conn_str, SUMMARY_SQL, (device, start_date.strftime("%Y-%m-%d"))
            )
    except Exception as e:
        st.error(f"Query failed: {e}")
        df_summary = pd.DataFrame()

    if df_summary.empty:
        st.warning("No data returned for this DEVICE / date filter.")
    else:
        int_cols = ["InputQty", "Wait", "Run", "PassQty", "FailQty"]
        for col in int_cols:
            if col in df_summary.columns:
                df_summary[col] = df_summary[col].fillna(0).astype(int)

        # KPI cards for Final test row
        ft_row = df_summary[df_summary["OPERATION"] == "Final test"]
        if not ft_row.empty:
            ft = ft_row.iloc[0]
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Final test - Input Qty", int(ft["InputQty"]))
            c2.metric("Wait", int(ft["Wait"]))
            c3.metric("Run", int(ft["Run"]))
            c4.metric("Pass Qty", int(ft["PassQty"]))
            c5.metric("Yield %", f"{ft['YieldPercent']:.2f}%")

        st.markdown("#### Station-by-station pareto")

        def highlight_yield(val):
            if isinstance(val, (int, float)):
                if val == 0:
                    return ""
                if val < 90:
                    return "background-color: #f7c1c1"
                if val < 98:
                    return "background-color: #fac775"
                return "background-color: #c0dd97"
            return ""

        styled = df_summary.style.applymap(highlight_yield, subset=["YieldPercent"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        csv = df_summary.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download summary as CSV", csv, "final_test_summary.csv", "text/csv"
        )


# ---------------------------------------------------------------------------
# Page 2: Device Tracker
# ---------------------------------------------------------------------------
with tab_devices:
    st.subheader(f"Devices in Wait / Run at Final test — DEVICE {device}, "
                  f"WO start >= {start_date}")

    try:
        with st.spinner("Running wait/run query..."):
            df_waitrun = run_query(
                conn_str, WAIT_RUN_SQL, (device, start_date.strftime("%Y-%m-%d"))
            )
    except Exception as e:
        st.error(f"Wait/Run query failed: {e}")
        df_waitrun = pd.DataFrame()

    if df_waitrun.empty:
        st.info("No devices currently in Wait/Run at Final test for this filter.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Total devices", df_waitrun["COMPONENTID"].nunique())
        c2.metric("Wait", int((df_waitrun["STATUS"] == "Wait").sum()))
        c3.metric("Run", int((df_waitrun["STATUS"] == "Run").sum()))

        component_ids = df_waitrun["COMPONENTID"].dropna().unique().tolist()

        # Pull a per-component Final test summary (EQUPMENT/Result/FailureCodeID)
        df_show = df_waitrun.copy()
        if component_ids:
            try:
                sql, params = device_summary_sql(component_ids)
                df_sum = run_query(conn_str, sql, tuple(params))
                df_show = df_show.merge(df_sum, on="COMPONENTID", how="left")
            except Exception as e:
                st.error(f"Summary query failed: {e}")

        st.markdown("#### Lot / Status overview")
        st.caption("Click any row to view full test data for that device.")

        event = st.dataframe(
            df_show,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="device_table"
        )

        if component_ids:
            @st.dialog("Latest Final test detail")
            def show_component_detail(comp_id: str):
                row = df_show[df_show["COMPONENTID"] == comp_id].iloc[0]
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**COMPONENTID**\n\n{comp_id}")
                c2.markdown(f"**Result**\n\n{row.get('Result', 'N/A')}")
                c3.markdown(f"**EQUPMENT**\n\n{row.get('EQUPMENT', 'N/A')}")

                col4, col5 = st.columns(2)
                col4.markdown(f"**Times Tested (ATS_RT)**\n\n{row.get('TimesTested', 'N/A')}")
                fail_channels = row.get("FailChannels")
                fail_codes = row.get("FailureCodes")
                col5.markdown(f"**Fail Channels**\n\n{fail_channels if pd.notna(fail_channels) else 'None'}")
                st.markdown(f"**Failure Codes**\n\n{fail_codes if pd.notna(fail_codes) else 'None'}")

                st.markdown("---")

                try:
                    df_detail = run_query(conn_str, DEVICE_DETAIL_SQL, (comp_id,))
                except Exception as e:
                    st.error(f"Query failed: {e}")
                    df_detail = pd.DataFrame()

                if df_detail.empty:
                    st.info("No test data found for this device.")
                else:
                    def highlight_fail(val):
                        if isinstance(val, str) and val.strip().upper() in ("FAIL", "Fail"):
                            return "background-color: #f7c1c1"
                        if isinstance(val, str) and val.strip().upper() in ("PASS", "Pass"):
                            return "background-color: #c0dd97"
                        return ""

                    styled = df_detail.style.applymap(
                        highlight_fail, subset=["CH_Pass_Fail"]
                    )
                    st.dataframe(styled, use_container_width=True, hide_index=True)

            # Open dialog when a row is selected
            selected_rows = event.selection.get("rows", [])
            if selected_rows:
                selected_idx = selected_rows[0]
                if selected_idx < len(df_show):
                    comp_id = df_show.iloc[selected_idx]["COMPONENTID"]
                    show_component_detail(comp_id)


# ---------------------------------------------------------------------------
# Page 3: Station Daily Report
# ---------------------------------------------------------------------------
with tab_station:
    st.subheader("Station Daily Report")

    # Date range pickers
    col1, col2 = st.columns(2)
    with col1:
        from_date = st.date_input("From date", value=date.today())
    with col2:
        to_date = st.date_input("To date", value=date.today())

    from_date_str = from_date.strftime("%Y-%m-%d")
    to_date_str = to_date.strftime("%Y-%m-%d")

    # Clear cached station data when date range changes
    date_key = (from_date_str, to_date_str)
    if st.session_state.get("station_date_key") != date_key:
        for k in ("df_station_cache", "df_golden_cache", "df_failed_cache",
                  "df_merged_cache"):
            st.session_state.pop(k, None)

    # ── SQL templates (defined once, used in button block below) ─────────────
    STATION_SUMMARY_SQL = """
    WITH LatestPerDevice AS (
        SELECT
            COMPONENTID,
            EQUPMENT,
            TESTRESULT,
            ROW_NUMBER() OVER (
                PARTITION BY COMPONENTID, EQUPMENT
                ORDER BY CREATETIME DESC
            ) AS rn
        FROM [MES].[dbo].[TESTRESULT_800G_MASTER]
        WHERE OPERATION = 'Final test'
          AND CAST(CREATETIME AS date) >= ?
          AND CAST(CREATETIME AS date) <= ?
    )
    SELECT
        EQUPMENT,
        COUNT(*) AS DevicesTested,
        SUM(CASE WHEN UPPER(LTRIM(RTRIM(TESTRESULT))) = 'PASS' THEN 1 ELSE 0 END) AS PassQty,
        SUM(CASE WHEN UPPER(LTRIM(RTRIM(TESTRESULT))) = 'FAIL' THEN 1 ELSE 0 END) AS FailQty,
        CAST(
            SUM(CASE WHEN UPPER(LTRIM(RTRIM(TESTRESULT))) = 'PASS' THEN 1 ELSE 0 END) * 100.0
            / NULLIF(COUNT(*), 0)
        AS decimal(10,2)) AS YieldPct
    FROM LatestPerDevice
    WHERE rn = 1
    GROUP BY EQUPMENT
    ORDER BY EQUPMENT
    """

    GS_SLOTS = ['99826B20030', '18625L60001', 'P172253403413', 'P172253900810']

    GS_SUMMARY_SQL = """
    WITH Ranked AS (
        SELECT
            LOT AS GoldenSampleID,
            EQUPMENT,
            TESTNUMBER,
            USERID,
            TESTRESULT,
            CREATETIME,
            ROW_NUMBER() OVER (
                PARTITION BY LOT, EQUPMENT
                ORDER BY TESTNUMBER DESC
            ) AS rn
        FROM [MES].[dbo].[TESTRESULT_AOC_MASTER]
        WHERE LOT IN (?,?,?,?)
          AND CAST(CREATETIME AS date) >= ?
          AND CAST(CREATETIME AS date) <= ?
    )
    SELECT
        GoldenSampleID,
        EQUPMENT,
        TESTNUMBER,
        USERID,
        TESTRESULT AS OverallResult,
        CREATETIME AS RunTime
    FROM Ranked
    WHERE rn = 1
    ORDER BY EQUPMENT, GoldenSampleID
    """

    GS_DELTA_SQL = """
    SELECT *
    FROM [MES].[dbo].[TestResult_800G_2XFR4_Golden_Sample_TEST]
    WHERE COMPONENTID = ?
      AND TESTNUMBER = (
          SELECT TOP 1 TESTNUMBER
          FROM [MES].[dbo].[TESTRESULT_AOC_MASTER]
          WHERE LOT = ?
            AND EQUPMENT = ?
            AND CAST(CREATETIME AS date) >= ?
            AND CAST(CREATETIME AS date) <= ?
          ORDER BY TESTNUMBER DESC
      )
    ORDER BY CHNumber
    """

    FAILED_DEVICES_SQL = """
    WITH LatestPerDevice AS (
        SELECT
            COMPONENTID,
            EQUPMENT,
            TESTRESULT,
            USERID,
            CREATETIME,
            ROW_NUMBER() OVER (
                PARTITION BY COMPONENTID, EQUPMENT
                ORDER BY CREATETIME DESC
            ) AS rn
        FROM [MES].[dbo].[TESTRESULT_800G_MASTER]
        WHERE OPERATION = 'Final test'
          AND CAST(CREATETIME AS date) >= ?
          AND CAST(CREATETIME AS date) <= ?
    ),
    FailedDevices AS (
        SELECT COMPONENTID, EQUPMENT, USERID, CREATETIME
        FROM LatestPerDevice
        WHERE rn = 1
          AND UPPER(LTRIM(RTRIM(TESTRESULT))) = 'FAIL'
    ),
    TrxRanked AS (
        SELECT
            T.COMPONENTID,
            T.CHNumber,
            T.CH_Pass_Fail,
            T.FailureCodeID,
            ROW_NUMBER() OVER (
                PARTITION BY T.COMPONENTID, T.CHNumber
                ORDER BY T.TESTNUMBER DESC
            ) AS rn
        FROM [MES].[dbo].[TestResult_800G_2XFR4_TRX_TEST] T
        WHERE T.COMPONENTID IN (SELECT COMPONENTID FROM FailedDevices)
    ),
    TrxFails AS (
        SELECT COMPONENTID, CHNumber, FailureCodeID
        FROM TrxRanked
        WHERE rn = 1
          AND UPPER(LTRIM(RTRIM(CH_Pass_Fail))) = 'FAIL'
    ),
    TrxFailsAgg AS (
        SELECT
            COMPONENTID,
            STRING_AGG(CHNumber, ' | ') WITHIN GROUP (ORDER BY CHNumber) AS FailChannels,
            STRING_AGG(
                CHNumber + ': ' + ISNULL(NULLIF(LTRIM(RTRIM(FailureCodeID)),'0'), 'N/A'),
                ' | '
            ) WITHIN GROUP (ORDER BY CHNumber) AS FailureCodes
        FROM TrxFails
        GROUP BY COMPONENTID
    ),
    RunCounts AS (
        SELECT T.COMPONENTID,
               COUNT(DISTINCT T.TESTNUMBER) AS TimesTested
        FROM [MES].[dbo].[TestResult_800G_2XFR4_TRX_TEST] T
        INNER JOIN [MES].[dbo].[TESTRESULT_800G_MASTER] M
            ON M.COMPONENTID = T.COMPONENTID
           AND M.TESTNUMBER  = T.TESTNUMBER
           AND M.OPERATION   = 'Final test'
           AND CAST(M.CREATETIME AS date) >= ?
           AND CAST(M.CREATETIME AS date) <= ?
        WHERE T.COMPONENTID IN (SELECT COMPONENTID FROM FailedDevices)
          AND T.CHNumber LIKE '%ATS_RT%'
        GROUP BY T.COMPONENTID
    )
    SELECT
        F.EQUPMENT,
        F.COMPONENTID,
        F.USERID,
        F.CREATETIME AS RunTime,
        FA.FailureCodes,
        ISNULL(RC.TimesTested, 0) AS TimesTested
    FROM FailedDevices F
    LEFT JOIN TrxFailsAgg FA ON FA.COMPONENTID = F.COMPONENTID
    LEFT JOIN RunCounts RC ON RC.COMPONENTID = F.COMPONENTID
    ORDER BY F.EQUPMENT, F.COMPONENTID
    """

    # ── Button: fetch data and store in session_state ─────────────────────────
    if st.button("Load Station Report", type="primary"):
        st.session_state["station_date_key"] = date_key

        # 1. Station summary
        try:
            with st.spinner("Loading station summary..."):
                df_station = run_query(
                    conn_str, STATION_SUMMARY_SQL,
                    (from_date_str, to_date_str)
                )
            st.session_state["df_station_cache"] = df_station
        except Exception as e:
            st.error(f"Station summary query failed: {e}")
            df_station = pd.DataFrame()

        # 2. Golden sample
        try:
            with st.spinner("Loading golden sample status..."):
                df_golden = run_query(
                    conn_str, GS_SUMMARY_SQL,
                    tuple(GS_SLOTS) + (from_date_str, to_date_str)
                )
            st.session_state["df_golden_cache"] = df_golden
        except Exception as e:
            st.error(f"Golden sample query failed: {e}")
            df_golden = pd.DataFrame()

        # 3. Build df_merged (station + GS done/not-done)
        if not df_station.empty:
            if not df_golden.empty:
                gs_equip_done = df_golden["EQUPMENT"].dropna().unique().tolist()
                df_merged = df_station.copy()
                df_merged["GoldenSampleStatus"] = df_merged["EQUPMENT"].apply(
                    lambda e: "Done" if e in gs_equip_done else "Not Done"
                )
            else:
                df_merged = df_station.copy()
                df_merged["GoldenSampleStatus"] = "Not Done"
            st.session_state["df_merged_cache"] = df_merged

        # 4. Failed devices
        if not df_station.empty:
            try:
                with st.spinner("Loading failed devices..."):
                    df_failed = run_query(
                        conn_str, FAILED_DEVICES_SQL,
                        (from_date_str, to_date_str,
                         from_date_str, to_date_str)
                    )
                st.session_state["df_failed_cache"] = df_failed
            except Exception as e:
                st.error(f"Failed devices query failed: {e}")

    # ── Display — reads from session_state so it survives row-click reruns ────
    if "df_station_cache" in st.session_state:
        df_station = st.session_state["df_station_cache"]
        df_golden = st.session_state.get("df_golden_cache", pd.DataFrame())
        df_merged = st.session_state.get("df_merged_cache", pd.DataFrame())
        df_failed = st.session_state.get("df_failed_cache", pd.DataFrame())

        if df_station.empty:
            st.warning(f"No Final test activity found between {from_date} and {to_date}.")
        else:
            # ── 3. Device count table ─────────────────────────────────────────
            st.markdown(f"#### Device counts by equipment — {from_date} to {to_date}")

            def highlight_gs(val):
                if val == "Done":
                    return "background-color: #c0dd97"
                if val == "Not Done":
                    return "background-color: #f7c1c1"
                return ""

            def highlight_yield(val):
                if not isinstance(val, (int, float)):
                    return ""
                if val < 90:
                    return "background-color: #f7c1c1"
                if val < 98:
                    return "background-color: #fac775"
                return "background-color: #c0dd97"

            if not df_merged.empty:
                styled_station = (
                    df_merged.style
                    .applymap(highlight_gs, subset=["GoldenSampleStatus"])
                    .applymap(highlight_yield, subset=["YieldPct"])
                )
                st.dataframe(styled_station, use_container_width=True, hide_index=True)

                c1, c2, c3 = st.columns(3)
                c1.metric("Stations active", df_merged["EQUPMENT"].nunique())
                c2.metric("Total devices tested", int(df_merged["DevicesTested"].sum()))
                gs_done = (df_merged["GoldenSampleStatus"] == "Done").sum()
                c3.metric("Golden sample done", f"{gs_done}/{df_merged.shape[0]} stations")

            # ── Failed devices table ──────────────────────────────────────────
            st.markdown("---")
            st.markdown("#### Failed devices")

            if df_failed.empty:
                st.info("No failed devices found for this date range.")
            else:
                st.caption(f"{len(df_failed)} failed device(s) — click a row to view full test data.")

                @st.dialog("Failed device — test detail")
                def show_failed_detail(comp_id: str):
                    st.markdown(f"**COMPONENTID:** {comp_id}")
                    try:
                        df_fd = run_query(conn_str, DEVICE_DETAIL_SQL, (comp_id,))
                    except Exception as e:
                        st.error(f"Query failed: {e}")
                        df_fd = pd.DataFrame()
                    if df_fd.empty:
                        st.info("No test data found.")
                    else:
                        def hl_fail(val):
                            if isinstance(val, str) and val.strip().upper() == "FAIL":
                                return "background-color: #f7c1c1"
                            if isinstance(val, str) and val.strip().upper() == "PASS":
                                return "background-color: #c0dd97"
                            return ""
                        st.dataframe(
                            df_fd.style.applymap(hl_fail, subset=["CH_Pass_Fail"]),
                            use_container_width=True, hide_index=True
                        )

                fail_event = st.dataframe(
                    df_failed,
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="failed_table_p3"
                )
                sel = fail_event.selection.get("rows", [])
                if sel:
                    show_failed_detail(df_failed.iloc[sel[0]]["COMPONENTID"])

            # ── 4. Golden sample summary table ────────────────────────────────
            st.markdown("---")
            st.markdown("#### Golden sample runs")

            if df_golden.empty:
                st.info("No golden sample records found for this date range.")
            else:
                def hl_gs_result(val):
                    if isinstance(val, str):
                        if val.strip().upper() == "PASS":
                            return "background-color: #c0dd97"
                        if val.strip().upper() == "FAIL":
                            return "background-color: #f7c1c1"
                    return ""

                styled_gs = df_golden.style.applymap(hl_gs_result, subset=["OverallResult"])
                st.dataframe(styled_gs, use_container_width=True, hide_index=True)

                # ── 5. Delta TXP drill-down per golden sample slot ────────────
                st.markdown("#### Golden sample latest run — delta details")
                col_gs, col_eq = st.columns(2)
                with col_gs:
                    gs_options = df_golden["GoldenSampleID"].tolist()
                    sel_gs = st.selectbox("Golden sample slot", options=gs_options,
                                           key="gs_slot_sel")
                with col_eq:
                    equip_options = df_merged["EQUPMENT"].dropna().unique().tolist()
                    sel_gs_equip = st.selectbox("Equipment", options=equip_options,
                                                 key="gs_equip_sel")

                if sel_gs and sel_gs_equip:
                    try:
                        df_delta = run_query(
                            conn_str, GS_DELTA_SQL,
                            (sel_gs, sel_gs, sel_gs_equip, from_date_str, to_date_str)
                        )
                    except Exception as e:
                        st.error(f"Delta query failed: {e}")
                        df_delta = pd.DataFrame()

                    if not df_delta.empty:
                        delta_cols = ["Detal_TXP", "Detal_Sensitivity", "Detal_ER",
                                      "Detal_TDECQ", "Detal_Temp", "Detal_Vcc",
                                      "Detal_Rxp", "Delta_Case_Temp"]
                        for c in delta_cols:
                            if c in df_delta.columns:
                                df_delta[c] = pd.to_numeric(df_delta[c], errors="coerce")

                        def hl_delta(val):
                            if pd.isna(val):
                                return ""
                            if abs(val) > 1:
                                return "background-color: #f7c1c1; font-weight: bold"
                            return "background-color: #c0dd97"

                        def hl_result(val):
                            if isinstance(val, str):
                                if val.strip().upper() == "PASS":
                                    return "background-color: #c0dd97"
                                if val.strip().upper() == "FAIL":
                                    return "background-color: #f7c1c1"
                            return ""

                        existing_delta = [c for c in delta_cols if c in df_delta.columns]
                        styled_delta = (
                            df_delta.style
                            .applymap(hl_result, subset=["CH_Pass_Fail"])
                            .applymap(hl_delta, subset=existing_delta)
                        )
                        st.caption(f"Latest run for **{sel_gs}** on **{sel_gs_equip}** — "
                                   f"delta columns highlighted red if |value| > 1")
                        st.dataframe(styled_delta, use_container_width=True, hide_index=True)
                    else:
                        st.info(f"No golden sample data found for {sel_gs} on {sel_gs_equip} "
                                f"between {from_date} and {to_date}.")