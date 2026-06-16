# Final Test Tracker

A Streamlit app that connects directly to the MES SQL Server and provides:

- **Page 1 — Summary**: station-by-station pareto (Wait/Run/Pass/Fail/Yield%)
  for the StartRuncard → INVT_PCS sequence, for a given DEVICE and WO start date.
- **Page 2 — Device Tracker**: every device currently sitting in Wait/Run at
  Final test, joined with its latest TESTRESULT_800G_MASTER row (all columns).

## Setup

1. Install Python 3.9+ on a machine that has network access to the MES SQL
   Server, and has the **Microsoft ODBC Driver for SQL Server** installed
   (driver 17 or 18). Download: https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:

   ```bash
   streamlit run app.py
   ```

4. In the sidebar:
   - Enter the SQL Server hostname/IP and database name (default `MES`)
   - Choose Windows Authentication (if running on a domain machine with
     access) or enter SQL login credentials
   - Enter the DEVICE and start date you want to filter on
   - Click **Connect / Refresh**

## Notes / things you may need to adjust

- The queries assume `v_MasterWOInfo`, `V_MasterRuncardInfo`, and
  `[MES].[dbo].[TESTRESULT_800G_MASTER]` are all in the same database
  specified in the sidebar. If `v_MasterWOInfo` / `V_MasterRuncardInfo`
  live in a different database, prefix them with the DB name
  (e.g. `[OtherDB].[dbo].[v_MasterWOInfo]`) in `app.py`.
- The "latest Final test" lookup pulls **all columns** from
  `TESTRESULT_800G_MASTER` for the most recent row per COMPONENTID where
  `OPERATION = 'Final test'`. If a device hasn't started Final test yet,
  it won't appear in that table (you'll see a warning).
- For security, avoid hardcoding passwords in `app.py`. If you need a
  saved login, consider using `st.secrets` (a `secrets.toml` file) instead
  of typing credentials each session.
- To deploy for your team, you can run this on a shared machine/VM with
  `streamlit run app.py --server.port 8501 --server.address 0.0.0.0` and
  share the URL internally (assuming the host has DB access).
