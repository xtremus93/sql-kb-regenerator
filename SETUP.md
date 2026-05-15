# SQL Server Knowledge-Base Regenerator — Setup Guide

---

## 1. Prerequisites

### Operating System
Windows 10 / Windows Server 2016 or later (Windows Authentication requires the host to be domain-joined or locally trusted by the SQL Server instance).  
Linux is supported for Kerberos-authenticated environments (see ODBC driver notes below).

### Python Version
Python **3.10** or later is required.  
Verify with:
```
python --version
```

### ODBC Driver Installation

The regenerator tries **ODBC Driver 18 for SQL Server** first, then falls back to **Driver 17**.  
Install at least one of the following:

**Windows:**
- Download from: https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
- Run the installer for `msodbcsql18.msi` (Driver 18) or `msodbcsql17.msi` (Driver 17).
- Verify installation:
  ```powershell
  Get-OdbcDriver | Where-Object { $_.Name -like "*SQL Server*" }
  ```

**Linux (Ubuntu/Debian):**
```bash
# Import the Microsoft signing key
curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
# Add the repository (example: Ubuntu 22.04)
curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
# Install Driver 18
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18
# Install Driver 17 (fallback)
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql17
```

### SQL Server Login Requirements

The regenerator uses **Windows Authentication** (Integrated Security). The account running the process must have:

| Permission | Object | Notes |
|------------|--------|-------|
| `SELECT` | `sys.objects` | Object inventory |
| `SELECT` | `sys.schemas` | Schema names |
| `SELECT` | `sys.columns` | Column metadata |
| `SELECT` | `sys.indexes`, `sys.index_columns` | Index metadata |
| `SELECT` | `sys.sql_expression_dependencies` | Dependency graph |
| `SELECT` | `sys.dm_db_partition_stats` | Row counts / sizes |
| `SELECT` | `msdb.dbo.sysjobs` | SQL Agent jobs |
| `SELECT` | `msdb.dbo.sysjobsteps` | Job steps |
| `SELECT` | `msdb.dbo.sysjobhistory` | Job run history |
| `SELECT` | `msdb.dbo.sysschedules` | Job schedules |
| `SELECT` | `msdb.dbo.syscategories` | Job categories |
| `VIEW SERVER STATE` | Server-level | Required for `sys.dm_db_partition_stats` |

A recommended minimal SQL Server role setup:
```sql
-- Grant db_datareader on target database
USE ContactCenter;
ALTER ROLE db_datareader ADD MEMBER [DOMAIN\svc_regenerator];

-- Grant VIEW SERVER STATE at server level
GRANT VIEW SERVER STATE TO [DOMAIN\svc_regenerator];

-- Grant SELECT on msdb tables
USE msdb;
GRANT SELECT ON msdb.dbo.sysjobs        TO [DOMAIN\svc_regenerator];
GRANT SELECT ON msdb.dbo.sysjobsteps    TO [DOMAIN\svc_regenerator];
GRANT SELECT ON msdb.dbo.sysjobhistory  TO [DOMAIN\svc_regenerator];
GRANT SELECT ON msdb.dbo.sysschedules   TO [DOMAIN\svc_regenerator];
GRANT SELECT ON msdb.dbo.syscategories  TO [DOMAIN\svc_regenerator];
```

---

## 2. Installation

### Step 1 — Copy the package
Place the project directory on your machine. The directory layout should be:
```
Py_SQL_Reader/
├── regenerator/
│   ├── __init__.py
│   ├── __main__.py
│   ├── algorithm.py
│   ├── archive.py
│   ├── connection.py
│   ├── hashing.py
│   ├── logger.py
│   ├── mirror.py
│   ├── notes.py
│   ├── queries.py
│   └── renderer.py
├── requirements.txt
└── SETUP.md
```

### Step 2 — Create a virtual environment
```powershell
cd D:\Documentos\Claude Code\Py_SQL_Reader
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows PowerShell
# or on Linux/macOS:
# source .venv/bin/activate
```

### Step 3 — Install dependencies
```powershell
pip install -r requirements.txt
```

**Dependency list (pinned versions in requirements.txt):**

| Package | Version | Purpose |
|---------|---------|---------|
| `pyodbc` | ≥4.0.39 | SQL Server connectivity via ODBC |
| `ruamel.yaml` | ≥0.18.6 | YAML front matter serialization (preserves field order, block style) |

---

## 3. Configuration

All configuration is passed via the command-line interface. There is no configuration file.

### Server and database targets
Use `--server` and `--database` as paired repeatable arguments. The **Nth `--server`** is paired with the **Nth `--database`**. Mismatched counts abort immediately.

```
--server SQLPROD01 --database ContactCenter
--server SQLPROD02 --database HR
```

### `--mode full` vs `--mode incremental`

| Mode | Behaviour |
|------|-----------|
| `full` | All objects are reclassified as MODIFIED before extraction. Every file is re-rendered and re-written (subject to SHA-256 idempotency check). Use for the first run or after schema changes that may not update `modify_date`. |
| `incremental` | Only NEW and MODIFIED objects (where `sql_modify_date` in the database is strictly greater than the value stored in the existing file's YAML) are re-extracted. UNCHANGED objects are never re-queried. Use for routine scheduled runs. |

### `--output-root`
Root directory where all Markdown files are written. Created automatically if it does not exist. Example: `--output-root ./kb` writes to `./kb/sqlprod01/contactcenter/`.

---

## 4. Running the Regenerator

### Example 1 — Single target, full mode
Re-documents the ContactCenter database from scratch:
```powershell
python -m regenerator `
  --server SQLPROD01 `
  --database ContactCenter `
  --mode full `
  --output-root ./kb
```

### Example 2 — Multi-target, incremental mode
Documents two databases in one run, skipping unchanged objects:
```powershell
python -m regenerator `
  --server SQLPROD01 --database ContactCenter `
  --server SQLPROD02 --database HR `
  --mode incremental `
  --output-root ./kb
```

### Example 3 — Multi-target, full mode with custom output root
```powershell
python -m regenerator `
  --server SQLPROD01 --database ContactCenter `
  --server SQLPROD01 --database Billing `
  --server SQLPROD02 --database HR `
  --mode full `
  --output-root D:\KnowledgeBase\SQL
```

**Linux equivalent (bash):**
```bash
python -m regenerator \
  --server SQLPROD01 --database ContactCenter \
  --server SQLPROD02 --database HR \
  --mode incremental \
  --output-root ./kb
```

---

## 5. Output Structure

For each `{server}/{database}` target the regenerator produces the following tree under `--output-root`:

```
kb/
└── sqlprod01/
    └── contactcenter/
        ├── _index.md                  ← Manifest: object inventory + counts
        ├── tables.md                  ← All tables in one file (table catalog)
        ├── views/
        │   └── sqlprod01.contactcenter.reporting.vw_nps_summary.md
        ├── procedures/
        │   ├── sqlprod01.contactcenter.dbo.usp_load_factscorecard.md
        │   └── sqlprod01.contactcenter.dbo.usp_import_nps.md
        ├── functions/
        │   └── sqlprod01.contactcenter.dbo.fn_calculateaht.md
        ├── triggers/
        │   └── sqlprod01.contactcenter.dbo.trg_factscorecard_audit.md
        ├── jobs/
        │   └── sqlprod01.msdb.dbo.nightly_nps_load.md
        └── _archive/
            ├── views/
            ├── procedures/
            │   └── sqlprod01.contactcenter.dbo.usp_old_nps_loader.md
            ├── functions/
            ├── triggers/
            ├── table/
            └── jobs/
```

**Key conventions:**
- File names use the full slug: `{server}.{database}.{schema}.{object_name_lowercase}.md`
- `tables.md` aggregates all tables in one file; it is not split per table
- Archived objects are moved to `_archive/{object_type}/` with `deprecated: true` set in YAML
- Jobs are filed under the database whose procedures they most invoke

---

## 6. Exit Codes and Log Interpretation

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | All targets completed without any `[WARN]` or `[ERROR]`. Idempotency guaranteed. |
| `1`  | One or more warnings or errors occurred. Check stdout/stderr for details. |

### Log line prefixes

| Prefix | Meaning |
|--------|---------|
| `[INFO]` | Normal progress. Emitted after each step completes for each target. |
| `[WARN]` | Non-fatal issue. Processing continued but output may be incomplete. |
| `[ERROR]` | Fatal issue for the affected `{server}/{database}` pair. That pair was aborted. |
| `[SUMMARY]` | Final per-target counts emitted after all targets are processed. |

### Common `[WARN]` messages and causes

| Message | Likely Cause |
|---------|-------------|
| `Cannot connect to SQLPROD02: ...` | Network unreachable, SQL Server stopped, ODBC driver not found, or firewall blocking port 1433. |
| `Linked server alias 'PROD02_LINK' in ... is not registered in sys.servers.` | A cross-server reference appears in object source code but the linked server is not configured on this instance. |
| `Cannot query SQL Agent jobs for SQLPROD01: ...` | The login lacks SELECT permission on `msdb.dbo.sysjobs`, or SQL Server Agent is not running. |
| `Malformed Human Notes delimiters in ...` | An existing MD file has `<!-- HUMAN_NOTES_START -->` without `<!-- HUMAN_NOTES_END -->` (or reversed). The body is preserved verbatim; only YAML front matter is updated. |

### Common `[ERROR]` messages and causes

| Message | Likely Cause |
|---------|-------------|
| `Missing permission on sys.objects for login.` | The login does not have SELECT on system views. Grant `db_datareader` or specific permissions. |
| `Missing permission or error querying table metadata for ...` | Missing permission on `sys.tables`, `sys.columns`, etc. |
| `Partial write failure on ...` | Disk full, output path not writable, or file locked by another process. Pre-write content is restored. |

### Reading `[SUMMARY]`
```
[SUMMARY] sqlprod01/contactcenter: 14 files written, 3 skipped (hash match), 1 archived
```
- **files written**: files where content changed and were written to disk
- **skipped (hash match)**: files rendered identically to what was on disk (idempotency)
- **archived**: objects moved to `_archive/`

---

## 7. Scheduling

### Windows Task Scheduler

Schedule a nightly incremental run at 03:00 using `schtasks`:

```powershell
schtasks /Create `
  /TN "SQL_KB_Regenerator_Nightly" `
  /TR "D:\Documentos\Claude Code\Py_SQL_Reader\.venv\Scripts\python.exe -m regenerator --server SQLPROD01 --database ContactCenter --server SQLPROD02 --database HR --mode incremental --output-root D:\KnowledgeBase\SQL" `
  /SC DAILY `
  /ST 03:00 `
  /RU "DOMAIN\svc_regenerator" `
  /RP `
  /F
```

> **Note:** Replace `DOMAIN\svc_regenerator` with the Windows account that has SQL Server access. The `/RP` flag prompts for the password. For a service account, use `/RU SYSTEM` if the machine account has SQL Server access, or store credentials in Windows Credential Manager.

To verify the task was created:
```powershell
schtasks /Query /TN "SQL_KB_Regenerator_Nightly" /FO LIST
```

### Linux (cron)

Add to crontab with `crontab -e`:

```cron
# Nightly incremental run at 03:00
0 3 * * * /opt/kb/Py_SQL_Reader/.venv/bin/python -m regenerator \
  --server SQLPROD01 --database ContactCenter \
  --server SQLPROD02 --database HR \
  --mode incremental \
  --output-root /var/data/KnowledgeBase/SQL \
  >> /var/log/sql_kb_regenerator.log 2>&1
```

> **Note:** On Linux, Windows Authentication requires Kerberos to be configured (`kinit` or a keytab file). Ensure the cron job runs under a user with a valid Kerberos ticket or keytab, and that `/etc/krb5.conf` is correctly set up for your domain.

---

## 8. Troubleshooting

### ODBC driver not found
**Symptom:** `[WARN] Cannot connect to SQLPROD01: ... 'ODBC Driver 18 for SQL Server' not found ...`  
**Solution:** Install the ODBC driver (see Section 1). On Windows, verify with:
```powershell
Get-OdbcDriver | Select-Object Name
```
On Linux:
```bash
odbcinst -q -d
```

### Windows Authentication failure
**Symptom:** `[WARN] Cannot connect to SQLPROD01: Login failed for user 'NT AUTHORITY\ANONYMOUS LOGON'`  
**Solutions:**
- Ensure the machine is domain-joined and the user running the process is a domain account
- Verify the SQL Server instance is reachable: `Test-NetConnection SQLPROD01 -Port 1433`
- Confirm the SQL login has at minimum `db_datareader` on the target database
- On Driver 18, if you see certificate errors, the connection string already includes `TrustServerCertificate=yes`

### Missing permission errors
**Symptom:** `[ERROR] Missing permission on sys.objects for login. Cannot extract metadata for SQLPROD01/ContactCenter.`  
**Solution:** Grant the required permissions listed in Section 1. The minimum is `db_datareader` on each target database plus `VIEW SERVER STATE` at server level:
```sql
USE ContactCenter;
ALTER ROLE db_datareader ADD MEMBER [DOMAIN\your_account];
GRANT VIEW SERVER STATE TO [DOMAIN\your_account];
```

### Partial write errors
**Symptom:** `[ERROR] Partial write failure on D:\KnowledgeBase\SQL\sqlprod01\contactcenter\views\...`  
**Solutions:**
- Check disk space: the output directory may be full
- Verify write permissions on the `--output-root` directory
- Check if any file is open and locked by another application (e.g., a text editor)
- The regenerator restores the pre-write content automatically on failure

### Malformed Human Notes delimiter warning
**Symptom:** `[WARN] Malformed Human Notes delimiters in .../views/sqlprod01....md. Body preserved verbatim. Manual review required.`  
**Cause:** The file has `<!-- HUMAN_NOTES_START -->` but is missing `<!-- HUMAN_NOTES_END -->`, or the delimiters appear in the wrong order.  
**Solution:** Open the file and ensure both delimiters appear on their own lines in the correct order:
```markdown
## Human Notes

<!-- HUMAN_NOTES_START -->
Your notes here.
<!-- HUMAN_NOTES_END -->
```

### Linked server not resolved
**Symptom:** `[WARN] Linked server alias 'PROD02_LINK' in sqlprod01.contactcenter.dbo.usp_load_factscorecard is not registered in sys.servers.`  
**Cause:** The stored procedure references a linked server that is not configured on the instance being documented.  
**Impact:** The cross-server dependency is recorded with `slug: null` and `unresolved: true` in the YAML. No other processing is affected.  
**Solution:** If the linked server should be resolvable, configure it on the SQL Server instance:
```sql
EXEC sp_addlinkedserver
  @server = 'PROD02_LINK',
  @srvproduct = '',
  @provider = 'SQLNCLI',
  @datasrc = 'SQLPROD02';
```
Re-run the regenerator after configuration.
