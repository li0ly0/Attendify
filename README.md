# Attendify: Attendance Monitoring Portal

**Attendify** is a sophisticated, Flask-based web application designed to interface with legacy Microsoft Access biometric databases (`.mdb`). It provides a modern, premium dark-themed UI for real-time monitoring, employee management, and attendance reporting.

---

## 🚀 Key Features

* **Real-time Dashboard:** Track live check-ins, late arrivals, and absent personnel at a glance.
* **Legacy DB Integration:** Direct connection to `att2000.mdb` using `pyodbc`.
* **Dynamic Shift Logic:** Supports custom weekday and weekend schedules, with automatic late and undertime detection.
* **DST (Daylight Savings) Management:** Granular control over temporal shifts on a per-department basis.
* **Admin Override System:** Manually edit or delete log entries to correct biometric errors.
* **Comprehensive Reporting:** Export filtered attendance logs and summaries directly to CSV.
* **Ultra-Modern UI:** Sleek glassmorphism design with Dark/Light mode support and AJAX-powered seamless pagination.

---

## 🛠️ Tech Stack

* **Backend:** Python / Flask
* **Database:** Microsoft Access (`pyodbc`) / JSON (for configurations)
* **Frontend:** HTML5, CSS3 (Glassmorphism), Vanilla JavaScript
* **Authentication:** Session-based RBAC (Admin/User roles)

---

## 📋 Prerequisites

1.  **Python 3.8+**
2.  **Microsoft Access Database Engine:** Required for `pyodbc` to read `.mdb` files.
3.  **ODBC Driver:** Ensure `Microsoft Access Driver (*.mdb)` is installed on the host machine.

---

## 🔧 Installation & Setup

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/your-username/attendify.git](https://github.com/your-username/attendify.git)
    cd attendify
    ```

2.  **Install dependencies:**
    ```bash
    pip install flask pyodbc
    ```

3.  **Configure Database Path:**
    Open `app.py` and update the `DB_PATH` variable to point to your live biometric database:
    ```python
    DB_PATH = r"C:\path\to\your\att2000_live.mdb"
    ```

4.  **Security Setup:**
    Update the `app.secret_key` and default passwords in the `USERS` dictionary before deployment.

5.  **Run the application:**
    ```bash
    python app.py
    ```
    The portal will be available at `http://localhost:5000`.

---

## 📂 File Structure

| File | Description |
| :--- | :--- |
| `app.py` | Main Flask application logic and database processing. |
| `employees.json` | Stores employee schedules, departments, and history. |
| `dst_settings.json` | Stores Daylight Savings configurations per department. |
| `log_overrides.json` | Tracks administrative manual edits/deletions. |
| `breakroom.json` | Stores Break Room reservation records. |

---

## 🔐 Credentials (Default)

| Role | Username | Password |
| :--- | :--- | :--- |
| **Administrator** | `admin` | `THC_adm1n` |
| **Standard User** | `user` | `thcus3r` |

---

## 🛠️ Management Logic

* **The 11 AM Rule:** The system defines a "logical workday" starting at 11:00 AM and ending at 10:59 AM the following day. This prevents late-night shifts from being split across two calendar dates.
* **Cooldown Period:** To prevent double-swiping, the system enforces a 3-minute cooldown between accepted logs.
* **Late Detection:** Lateness is calculated strictly based on the configured `time_in` plus a 1-minute grace period.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
