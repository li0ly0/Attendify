from flask import Flask, render_template, request, Response, redirect, url_for, session, jsonify
import pyodbc
from datetime import datetime, timedelta
import math
import csv
import io
import json
import os
from functools import wraps
import uuid

app = Flask(__name__)
# --- SECURITY CONFIGURATION ---
# Falls back to a default ONLY if the env var is missing (useful for local testing, 
# but ensure the env var is set in production)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fallback_dev_key_change_me')

# --- USER DATABASE (MOCK) ---
# Passwords are now pulled securely from environment variables
USERS = {
    'admin': {
        'password': os.environ.get('ADMIN_PASSWORD'), 
        'role': 'admin'
    },
    'user': {
        'password': os.environ.get('USER_PASSWORD'), 
        'role': 'user'
    }
}

# --- CONFIGURATION ---
# You can also move your DB path to the .env to make it easier to deploy to different machines
DB_PATH = os.environ.get('DB_PATH', r"")
JSON_PATH = "employees.json"
DST_JSON_PATH = "dst_settings.json"
LOG_OVERRIDES_PATH = "log_overrides.json" 

CONN_STR = (
    r'DRIVER={Microsoft Access Driver (*.mdb)};'
    f'DBQ={DB_PATH};'
    r'ReadOnly=1;'
    r'Exclusive=0;'
)

# --- DST CONFIGURATION ---
DST_OFFSET_HOURS = -1  # Use 1 to push time forward, or -1 to pull time back

# --- AUTH DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return render_template(
                'index.html', # fallback or return a simple string
                error="Access Denied. You need Administrator privileges to view this page."
            ), 403
        return f(*args, **kwargs)
    return decorated_function

# --- LOAD & SAVE JSON DATA ---
def load_employees():
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {} 
    return {}

def save_employees(data):
    with open(JSON_PATH, 'w') as f:
        json.dump(data, f, indent=4)

def load_dst_settings():
    if os.path.exists(DST_JSON_PATH):
        try:
            with open(DST_JSON_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {"start_date": "", "departments": {}}
    return {"start_date": "", "departments": {}}

def save_dst_settings(data):
    with open(DST_JSON_PATH, 'w') as f:
        json.dump(data, f, indent=4)

def load_log_overrides():
    if os.path.exists(LOG_OVERRIDES_PATH):
        try:
            with open(LOG_OVERRIDES_PATH, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_log_overrides(data):
    with open(LOG_OVERRIDES_PATH, 'w') as f:
        json.dump(data, f, indent=4)

# --- LOGIC HELPERS ---

def format_duration(total_seconds):
    if total_seconds <= 0:
        return ""
    
    days = int(total_seconds // 86400)
    rem = total_seconds % 86400
    hours = int(rem // 3600)
    rem %= 3600
    minutes = int(rem // 60)
    seconds = int(rem % 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days > 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if seconds > 0:
        parts.append(f"{seconds} second{'s' if seconds > 1 else ''}")
        
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    else:
        return ", ".join(parts[:-1]) + f", and {parts[-1]}"

def format_exceeded_time(seconds):
    return format_duration(seconds)

def format_ddhhmmss(total_seconds):
    return format_duration(total_seconds)

def calculate_break_param(time_str_in, time_str_out):
    if not time_str_in or not time_str_out:
        return None
    try:
        t_in = datetime.strptime(time_str_in, "%H:%M:%S")
        t_out = datetime.strptime(time_str_out, "%H:%M:%S")
        
        b_start = (t_in + timedelta(hours=1)).strftime("%H:%M:%S")
        b_end = (t_out - timedelta(seconds=1)).strftime("%H:%M:%S")
        
        return {"start": b_start, "end": b_end}
    except Exception:
        return None

def get_logical_date(dt):
    # Rule 1: A work day starts at 11am and ends at the following day 10:59am.
    if dt.hour < 11:
        return (dt - timedelta(days=1)).date()
    return dt.date()

def get_effective_schedule(emp_conf, logical_date):
    effective_sched = {
        'time_in': emp_conf.get('time_in'),
        'time_out': emp_conf.get('time_out'),
        'break_parameter': emp_conf.get('break_parameter'),
        'weekend_time_in': emp_conf.get('weekend_time_in'),
        'weekend_time_out': emp_conf.get('weekend_time_out'),
        'weekend_break_parameter': emp_conf.get('weekend_break_parameter')
    }

    if 'schedule_history' in emp_conf:
        history = sorted(
            emp_conf['schedule_history'], 
            key=lambda x: datetime.strptime(x['effective_date'], '%Y-%m-%d').date(), 
            reverse=True
        )
        for sched in history:
            if logical_date >= datetime.strptime(sched['effective_date'], '%Y-%m-%d').date():
                effective_sched.update(sched)
                break

    return effective_sched

def get_where_clause(employee_data):
    search_name = request.args.get('search_name', '').strip().lower()
    status_filter = request.args.get('status_filter', '').strip() 
    remark_filter = request.args.get('remark_filter', '').strip().upper()
    department_filter = request.args.get('department_filter', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    
    where_clauses = []
    params = []

    global_cutoff = datetime(2026, 3, 1, 11, 0, 0)

    if search_name:
        matching_ids = [bid for bid, info in employee_data.items() 
                        if search_name in info.get('name', '').lower()]
        
        if matching_ids:
            placeholders = ",".join(["?"] * len(matching_ids))
            where_clauses.append(f"(U.BADGENUMBER IN ({placeholders}) OR U.NAME LIKE ?)")
            params.extend(matching_ids)
            params.append(f"%{search_name}%")
        else:
            where_clauses.append("U.NAME LIKE ?")
            params.append(f"%{search_name}%")

    if date_from:
        dt_f = datetime.strptime(date_from, '%Y-%m-%d').replace(hour=11, minute=0, second=0)
        actual_start = max(dt_f, global_cutoff)
        where_clauses.append("C.CHECKTIME >= ?")
        params.append(actual_start)
    else:
        where_clauses.append("C.CHECKTIME >= ?")
        params.append(global_cutoff)
        
    if date_to:
        dt_t = datetime.strptime(date_to, '%Y-%m-%d')
        dt_t = dt_t.replace(hour=10, minute=59, second=59)
        where_clauses.append("C.CHECKTIME <= ?")
        params.append(dt_t)
    
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return where_sql, params, {
        "search_name": search_name, 
        "status_filter": status_filter,
        "remark_filter": remark_filter,
        "department_filter": department_filter,
        "date_from": date_from, "date_to": date_to
    }

def process_attendance_logs(all_rows, employee_data, filters=None, dst_settings=None):
    if filters is None: filters = {}
    grouped_logs = {}
    
    overrides = load_log_overrides()
    
    dst_start_dt = None
    if dst_settings and dst_settings.get('start_date'):
        try:
            dst_start_dt = datetime.strptime(dst_settings['start_date'] + " 12:00:00", '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    for r in all_rows:
        badge_id = str(r[1]) if r[1] else str(r[0])
        check_time = r[4]
        
        log_key = f"{badge_id}_{check_time.strftime('%Y%m%d%H%M%S')}"
        if overrides.get(log_key, {}).get("action") == "delete":
            continue

        logical_d = get_logical_date(check_time)
        key = (badge_id, logical_d)
        if key not in grouped_logs:
            grouped_logs[key] = []
        grouped_logs[key].append(r)

    processed_logs = []
    summary_dict = {}

    for b_id, emp in employee_data.items():
        summary_dict[b_id] = {
            'badge': b_id,
            'name': emp.get('name', 'Unknown'),
            'dept': emp.get('department', 'N/A'),
            'present': 0,
            'absent': 0,
            'late': 0,
            'total_late_seconds': 0,
            'overbreak': 0,
            'total_overbreak_seconds': 0,
            'overtime': 0,
            'total_overtime_seconds': 0,
            'undertime': 0,
            'total_undertime_seconds': 0,
            'total_checkouts': 0,
            'worked_days': set(),
            'no_checkout_dates': set()
        }

    COOLDOWN_MINUTES = 3 

    for (badge_id, logical_d), logs_in_shift in grouped_logs.items():
        logs_in_shift.sort(key=lambda x: x[4]) 
        emp_conf = employee_data.get(badge_id, {})
        
        active_sched = get_effective_schedule(emp_conf, logical_d)
        
        time_in_str = active_sched.get('time_in')
        time_out_str = active_sched.get('time_out')
        dept = emp_conf.get('department', 'N/A')
        break_param_dict = active_sched.get('break_parameter')

        is_weekend = logical_d.weekday() >= 5
        if is_weekend:
            if 'weekend_time_in' in active_sched:
                time_in_str = active_sched['weekend_time_in']
            if 'weekend_time_out' in active_sched:
                time_out_str = active_sched['weekend_time_out']
            
            if 'weekend_break_parameter' in active_sched and active_sched['weekend_break_parameter']:
                break_param_dict = active_sched['weekend_break_parameter']
        
        sched_time_in = None
        if time_in_str:
            t_in_hr = int(time_in_str.split(':')[0])
            in_date = logical_d + timedelta(days=1) if t_in_hr < 11 else logical_d
            sched_time_in = datetime.strptime(f"{in_date} {time_in_str}", '%Y-%m-%d %H:%M:%S')

        sched_time_out = None
        if time_out_str:
            t_out_hr = int(time_out_str.split(':')[0])
            out_date = logical_d + timedelta(days=1) if t_out_hr < 11 else logical_d
            sched_time_out = datetime.strptime(f"{out_date} {time_out_str}", '%Y-%m-%d %H:%M:%S')

        raw_break_start = None
        raw_break_end = None
        if break_param_dict and isinstance(break_param_dict, dict) and 'start' in break_param_dict and 'end' in break_param_dict:
            try:
                b_start_str = break_param_dict['start']
                b_start_hr = int(b_start_str.split(':')[0])
                b_start_date = logical_d + timedelta(days=1) if b_start_hr < 11 else logical_d
                raw_break_start = datetime.strptime(f"{b_start_date} {b_start_str}", '%Y-%m-%d %H:%M:%S')

                b_end_str = break_param_dict['end']
                b_end_hr = int(b_end_str.split(':')[0])
                b_end_date = logical_d + timedelta(days=1) if b_end_hr < 11 else logical_d
                raw_break_end = datetime.strptime(f"{b_end_date} {b_end_str}", '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass

        has_checked_in = False
        has_checked_out = False
        check_in_time = None
        in_break = False
        break_start_time = None
        total_break_seconds = 0
        last_break_idx = -1
        last_accepted_time = None
        
        shift_processed = []

        for r in logs_in_shift:
            check_time = r[4]
            log_key = f"{badge_id}_{check_time.strftime('%Y%m%d%H%M%S')}"
            
            if last_accepted_time:
                diff_minutes = (check_time - last_accepted_time).total_seconds() / 60
                if diff_minutes < COOLDOWN_MINUTES:
                    continue  
                    
            last_accepted_time = check_time
            db_name = r[2] if r[2] else f"User {r[0]}"
            check_type_hw = str(r[5]).strip().upper() if r[5] else ""
            name = emp_conf.get('name', db_name)
            
            apply_dst = False
            if dst_start_dt and check_time >= dst_start_dt:
                if dst_settings and dst_settings.get('departments', {}).get(dept, False):
                    apply_dst = True

            adj_sched_time_in = sched_time_in + timedelta(hours=DST_OFFSET_HOURS) if (sched_time_in and apply_dst) else sched_time_in
            adj_sched_time_out = sched_time_out + timedelta(hours=DST_OFFSET_HOURS) if (sched_time_out and apply_dst) else sched_time_out

            break_start_limit = raw_break_start + timedelta(hours=DST_OFFSET_HOURS) if (raw_break_start and apply_dst) else raw_break_start
            break_end_limit = raw_break_end + timedelta(hours=DST_OFFSET_HOURS) if (raw_break_end and apply_dst) else raw_break_end

            if not break_start_limit and adj_sched_time_in:
                break_start_limit = adj_sched_time_in + timedelta(hours=1)
            if not break_end_limit and adj_sched_time_out:
                break_end_limit = adj_sched_time_out - timedelta(seconds=1)

            display_type = 'Log'
            status = 'ON-TIME'
            special_remark = ''
            exceeded_text = ""
            late_seconds = 0
            overbreak_seconds = 0

            is_checkout_time = False
            if adj_sched_time_out and check_time >= adj_sched_time_out:
                is_checkout_time = True

            is_break_range = False
            if break_start_limit and break_end_limit:
                if break_start_limit <= check_time <= break_end_limit:
                    is_break_range = True

            if not has_checked_in:
                has_checked_in = True
                check_in_time = check_time
                display_type = 'Check In'
                
                if adj_sched_time_in:
                    if check_time < adj_sched_time_in + timedelta(minutes=1):
                        status = "ON-TIME"
                    else:
                        status = "LATE"
                        late_seconds = (check_time - adj_sched_time_in).total_seconds()
                else:
                    status = "ON-TIME"
                
            elif is_checkout_time:
                has_checked_out = True
                display_type = 'Check Out'

                if in_break:
                    break_duration = (check_time - break_start_time).total_seconds()
                    total_break_seconds += break_duration
                    in_break = False
                    special_remark = "No Break End"

                if adj_sched_time_in and adj_sched_time_out:
                    rendered = (check_time - check_in_time).total_seconds()
                    if rendered < (9 * 3600):
                        status = "UNDER TIME"
                    elif check_time >= adj_sched_time_out + timedelta(hours=1):
                        status = "OVERTIME"
                        exceeded_sec = (check_time - adj_sched_time_out).total_seconds()
                        exceeded_text = format_exceeded_time(exceeded_sec)
                    else:
                        status = "ON-TIME"
                else:
                    status = "ON-TIME"

            elif is_break_range:
                if not in_break:
                    in_break = True
                    break_start_time = check_time
                    display_type = 'Break Out'
                    status = "BREAK"
                else:
                    in_break = False
                    display_type = 'Break In' 
                    break_duration = (check_time - break_start_time).total_seconds()
                    old_total = total_break_seconds
                    total_break_seconds += break_duration
                    
                    if total_break_seconds >= 3660:
                        status = 'OVER BREAK'
                        incremental_overbreak = total_break_seconds - max(3600, old_total)
                        overbreak_seconds = incremental_overbreak
                        
                        exceeded_sec = total_break_seconds - 3600
                        exceeded_text = format_exceeded_time(exceeded_sec)
                    else:
                        status = 'BREAK'
                        
                last_break_idx = len(shift_processed)
            else:
                display_type = 'Log'
                status = 'ON-TIME'

            override_data = overrides.get(log_key)
            if override_data and override_data.get("action") == "edit":
                if "type" in override_data: display_type = override_data["type"]
                if "status" in override_data: status = override_data["status"]

            shift_processed.append({
                "BadgeID": badge_id,
                "Name": name,
                "Dept": dept,
                "Time": check_time,
                "LogKey": log_key,
                "Type": check_type_hw,
                "DisplayType": display_type,
                "Remark": status,
                "SpecialRemark": special_remark,
                "ExceededText": exceeded_text,
                "LateSeconds": late_seconds,
                "OverbreakSeconds": overbreak_seconds
            })
            
        if not has_checked_out and last_break_idx != -1:
            if shift_processed[last_break_idx].get("SpecialRemark"):
                shift_processed[last_break_idx]["SpecialRemark"] += ", No Check Out"
            else:
                shift_processed[last_break_idx]["SpecialRemark"] = "No Check Out"

        processed_logs.extend(shift_processed)

        if badge_id in summary_dict:
            summary_dict[badge_id]['worked_days'].add(logical_d)
            
            shift_in_time = None
            shift_out_time = None
            has_checkout_for_shift = False
            
            for sl in shift_processed:
                if sl['DisplayType'] == 'Check In':
                    if shift_in_time is None:
                        shift_in_time = sl['Time']
                    if sl['Remark'] == 'LATE':
                        summary_dict[badge_id]['late'] += 1
                        if adj_sched_time_in and sl['Time'] > adj_sched_time_in:
                            summary_dict[badge_id]['total_late_seconds'] += (sl['Time'] - adj_sched_time_in).total_seconds()
                            
                elif sl['DisplayType'] == 'Break In':
                    if sl['Remark'] == 'OVER BREAK':
                        summary_dict[badge_id]['overbreak'] += 1
                        summary_dict[badge_id]['total_overbreak_seconds'] += sl.get('OverbreakSeconds', 0)
                        
                elif sl['DisplayType'] == 'Check Out':
                    has_checkout_for_shift = True
                    shift_out_time = sl['Time']
                    summary_dict[badge_id]['total_checkouts'] += 1
                    if sl['Remark'] == 'OVERTIME':
                        summary_dict[badge_id]['overtime'] += 1
                        if adj_sched_time_out and sl['Time'] >= (adj_sched_time_out + timedelta(hours=1)):
                            summary_dict[badge_id]['total_overtime_seconds'] += (sl['Time'] - adj_sched_time_out).total_seconds()

            if not has_checkout_for_shift and len(shift_processed) > 0:
                summary_dict[badge_id]['no_checkout_dates'].add(logical_d)

            if not shift_out_time and shift_processed:
                shift_out_time = shift_processed[-1]['Time']

            if shift_in_time and shift_out_time and shift_out_time > shift_in_time:
                shift_rendered_secs = (shift_out_time - shift_in_time).total_seconds()
                
                is_undertime = any(s['Remark'] == 'UNDER TIME' for s in shift_processed)
                if is_undertime:
                    summary_dict[badge_id]['undertime'] += 1
                    if shift_rendered_secs < (9 * 3600):
                        summary_dict[badge_id]['total_undertime_seconds'] += ((9 * 3600) - shift_rendered_secs)

    processed_logs.sort(key=lambda x: x['Time'], reverse=True)

    start_logical_date = datetime.strptime(filters['date_from'], '%Y-%m-%d').date() if filters.get('date_from') else datetime(2026, 3, 1).date()
    
    if filters.get('date_to'):
        end_logical_date = datetime.strptime(filters['date_to'], '%Y-%m-%d').date() - timedelta(days=1)
    else:
        end_logical_date = get_logical_date(datetime.now())

    current_logical = get_logical_date(datetime.now())
    if end_logical_date > current_logical:
        end_logical_date = current_logical

    for b_id, emp in employee_data.items():
        if b_id not in summary_dict: continue
        
        emp_dept = emp.get('department', '').strip().upper()
        six_day_depts = ['DE', 'IT', 'UTILITY']
        is_six_day_worker = any(d == emp_dept for d in six_day_depts)
        
        expected_dates = []
        curr = start_logical_date
        while curr <= end_logical_date:
            if curr.weekday() < 5: 
                expected_dates.append(curr)
            elif curr.weekday() == 5 and is_six_day_worker: 
                expected_dates.append(curr)
            curr += timedelta(days=1)
            
        expected_days = len(expected_dates)
        worked_days_count = len(summary_dict[b_id]['worked_days'])
        summary_dict[b_id]['present'] = worked_days_count
        summary_dict[b_id]['absent'] = max(0, expected_days - worked_days_count)

        worked_dates_set = summary_dict[b_id]['worked_days']
        absent_dates = [d.strftime('%b %d') for d in expected_dates if d not in worked_dates_set]
        summary_dict[b_id]['absent_dates'] = ", ".join(absent_dates) if absent_dates else ""

        nc_dates = sorted(list(summary_dict[b_id]['no_checkout_dates']))
        formatted_nc = [d.strftime('%b %d') for d in nc_dates]
        summary_dict[b_id]['dates_no_checkout'] = ", ".join(formatted_nc) if formatted_nc else ""

    summary_list = sorted(summary_dict.values(), key=lambda x: x['name'])

    for summary in summary_list:
        summary['no_checkout'] = max(0, summary['present'] - summary['total_checkouts'])
        if summary['present'] == summary['total_checkouts']:
            summary['no_checkout'] = "" 
            
        adj_undertime = summary['total_undertime_seconds'] - summary['total_late_seconds']
        if adj_undertime > 0:
            summary['total_undertime_seconds'] = adj_undertime
        else:
            summary['total_undertime_seconds'] = 0

        summary['formatted_late'] = format_ddhhmmss(summary['total_late_seconds'])
        summary['formatted_overbreak'] = format_ddhhmmss(summary['total_overbreak_seconds'])
        summary['formatted_overtime'] = format_ddhhmmss(summary['total_overtime_seconds'])
        summary['formatted_undertime'] = format_ddhhmmss(summary['total_undertime_seconds'])
        
        summary['late'] = summary['late'] if summary['late'] > 0 else ""
        summary['overbreak'] = summary['overbreak'] if summary['overbreak'] > 0 else ""
        summary['overtime'] = summary['overtime'] if summary['overtime'] > 0 else ""
        summary['undertime'] = summary['undertime'] if summary['total_undertime_seconds'] > 0 else ""
        summary['absent'] = summary['absent'] if summary['absent'] > 0 else ""

    final_logs = []
    for log in processed_logs:
        if filters.get('remark_filter') and log['Remark'] != filters['remark_filter']:
            continue
        if filters.get('status_filter') and log['DisplayType'] != filters['status_filter']: 
            continue
        if filters.get('department_filter') and log['Dept'] != filters['department_filter']:
            continue
            
        final_logs.append(log)

    final_summary_list = []
    for summary in summary_list:
        if filters.get('department_filter') and summary['dept'] != filters['department_filter']:
            continue
            
        if filters.get('search_name'):
            s_name = filters['search_name']
            if s_name not in summary['name'].lower() and s_name not in str(summary['badge']).lower():
                continue

        final_summary_list.append(summary)

    return final_logs, final_summary_list

# --- ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username').lower()
        password = request.form.get('password')
        
        if username in USERS and USERS[username]['password'] == password:
            session['username'] = username
            session['role'] = USERS[username]['role']
            return redirect(url_for('index'))
        else:
            error = "Authentication failed. Invalid credentials."
            
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/update_dst', methods=['POST'])
@admin_required
def update_dst():
    data = request.json
    pwd = data.get('password')
    admin_user = session.get('username')
    
    if USERS.get(admin_user, {}).get('password') != pwd:
        return jsonify({'success': False, 'error': 'Invalid admin password'})

    dst = load_dst_settings()
    
    if 'start_date' in data:
        dst['start_date'] = data['start_date']
        
    if 'department' in data and 'state' in data:
        if 'departments' not in dst:
            dst['departments'] = {}
        dst['departments'][data['department']] = data['state']

    save_dst_settings(dst)
    return jsonify({'success': True})

@app.route('/modify_log', methods=['POST'])
@admin_required
def modify_log():
    action = request.form.get('action')
    log_key = request.form.get('log_key')
    
    overrides = load_log_overrides()
    
    if action == 'delete':
        overrides[log_key] = {"action": "delete"}
    elif action == 'edit':
        overrides[log_key] = {
            "action": "edit",
            "type": request.form.get('new_type'),
            "status": request.form.get('new_status')
        }
        
    save_log_overrides(overrides)
    return redirect(request.referrer or url_for('index'))

@app.route('/')
@login_required
def index():
    try:
        EMPLOYEE_DATA = load_employees()
        dst_settings = load_dst_settings()
        
        is_dst_active = any(dst_settings.get('departments', {}).values())
        
        page = request.args.get('page', 1, type=int)
        per_page = 10 
        where_sql, params, filters = get_where_clause(EMPLOYEE_DATA)

        departments_set = set(emp.get('department') for emp in EMPLOYEE_DATA.values() if emp.get('department'))
        departments = sorted(list(departments_set))
        
        conn = pyodbc.connect(CONN_STR, timeout=2)
        cursor = conn.cursor()

        sql = f"""
            SELECT 
                C.USERID, U.BADGENUMBER, U.NAME, D.DEPTNAME, C.CHECKTIME, C.CHECKTYPE
            FROM (CHECKINOUT C 
            LEFT JOIN USERINFO U ON C.USERID = U.USERID)
            LEFT JOIN DEPARTMENTS D ON U.DEFAULTDEPTID = D.DEPTID
            {where_sql}
        """
        cursor.execute(sql, params)
        all_rows = cursor.fetchall()
        
        all_logs, _ = process_attendance_logs(all_rows, EMPLOYEE_DATA, filters, dst_settings)
        recent_logs = all_logs[:5]

        total_filtered = len(all_logs)
        total_pages = math.ceil(total_filtered / per_page) if total_filtered > 0 else 1
        page_rows = all_logs[(page-1)*per_page : page*per_page]

        current_dt = datetime.now()
        logical_today = get_logical_date(current_dt)
        
        today_start = datetime(logical_today.year, logical_today.month, logical_today.day, 11, 0, 0)
        next_day = logical_today + timedelta(days=1)
        today_end = datetime(next_day.year, next_day.month, next_day.day, 10, 59, 59)

        global_cutoff = datetime(2026, 3, 1, 11, 0, 0)

        if filters.get('date_from'):
            df = datetime.strptime(filters['date_from'], '%Y-%m-%d')
            stat_start = datetime(df.year, df.month, df.day, 11, 0, 0)
            next_d = df + timedelta(days=1)
            stat_end = datetime(next_d.year, next_d.month, next_d.day, 10, 59, 59)
        else:
            stat_start = today_start
            stat_end = today_end

        if filters.get('date_to'):
            dt_t = datetime.strptime(filters['date_to'], '%Y-%m-%d')
            stat_end = datetime(dt_t.year, dt_t.month, dt_t.day, 10, 59, 59)

        actual_stat_start = max(stat_start, global_cutoff)

        filtered_emp_data = {}
        for b_id, emp in EMPLOYEE_DATA.items():
            if filters.get('search_name'):
                s_name = filters['search_name'].lower()
                if s_name not in emp.get('name', '').lower() and s_name not in str(b_id):
                    continue
            if filters.get('department_filter'):
                if emp.get('department') != filters['department_filter']:
                    continue
            filtered_emp_data[b_id] = emp

        cursor.execute("""
            SELECT C.USERID, U.BADGENUMBER, U.NAME, D.DEPTNAME, C.CHECKTIME, C.CHECKTYPE 
            FROM (CHECKINOUT C 
            LEFT JOIN USERINFO U ON C.USERID = U.USERID)
            LEFT JOIN DEPARTMENTS D ON U.DEFAULTDEPTID = D.DEPTID
            WHERE C.CHECKTIME >= ? AND C.CHECKTIME <= ?
            ORDER BY C.CHECKTIME ASC
        """, (actual_stat_start, stat_end))
        
        stat_rows = cursor.fetchall()
        stat_logs, _ = process_attendance_logs(stat_rows, filtered_emp_data, {}, dst_settings)
        
        present_badges = set()
        late_badges = set()
        
        for log in stat_logs:
            b_id = log['BadgeID']
            if b_id in filtered_emp_data:
                if log['DisplayType'] == 'Check In':
                    if log['Remark'] in ['ON-TIME', 'LATE']:
                        present_badges.add(b_id)
                    if log['Remark'] == 'LATE':
                        late_badges.add(b_id)

        total_employees = len(filtered_emp_data)
        present_count = len(present_badges)
        late_count = len(late_badges)
        
        eval_day = actual_stat_start.weekday()
        absent_employees_list = []
        for badge, data in filtered_emp_data.items():
            if badge not in present_badges:
                emp_dept = data.get('department', '').strip().upper()
                is_six_day_worker = any(d == emp_dept for d in ['DE', 'IT', 'UTILITY'])
                
                is_scheduled = True
                if eval_day == 6: 
                    is_scheduled = False
                elif eval_day == 5 and not is_six_day_worker:
                    is_scheduled = False
                    
                if is_scheduled:
                    absent_employees_list.append({
                        'badge': badge,
                        'name': data.get('name', f'Unknown User')
                    })
        
        absent_employees_list.sort(key=lambda x: x['name'])
        absent_count = len(absent_employees_list)

        absent_page = request.args.get('absent_page', 1, type=int)
        absent_per_page = 3
        total_absent = len(absent_employees_list)
        absent_total_pages = math.ceil(total_absent / absent_per_page) if total_absent > 0 else 1
        
        absent_start = (absent_page - 1) * absent_per_page
        absent_end = absent_page * absent_per_page
        paginated_absent = absent_employees_list[absent_start:absent_end]

        dashboard_stats = {
            "total": total_employees,
            "present": present_count,
            "late": late_count,
            "absent": absent_count,
            "total_logs": total_filtered
        }

        conn.close()
        search_active = any(filters.values())
        
        return render_template('index.html', 
            active_page='dashboard',
            logs=page_rows,
            recent_logs=recent_logs, 
            page=page, 
            total_pages=total_pages,
            absent_employees=paginated_absent,          
            absent_page=absent_page,                    
            absent_total_pages=absent_total_pages,      
            search_active=search_active, filters=filters,
            stats=dashboard_stats,
            departments=departments,
            dst_settings=dst_settings,
            is_dst_active=is_dst_active, 
            current_date=current_dt.strftime('%A, %B %d, %Y'),
            now=current_dt.strftime('%I:%M:%S %p'),
            db_path=DB_PATH)

    except Exception as e:
        return f"<div style='padding:40px; font-family: sans-serif; color: white; background: #000; height: 100vh;'><h2>System Error</h2><p>{e}</p></div>"

@app.route('/summary')
@admin_required
def attendance_summary_page():
    try:
        EMPLOYEE_DATA = load_employees()
        dst_settings = load_dst_settings()
        
        where_sql, params, filters = get_where_clause(EMPLOYEE_DATA)

        departments_set = set(emp.get('department') for emp in EMPLOYEE_DATA.values() if emp.get('department'))
        departments = sorted(list(departments_set))
        
        conn = pyodbc.connect(CONN_STR, timeout=2)
        cursor = conn.cursor()

        sql = f"""
            SELECT 
                C.USERID, U.BADGENUMBER, U.NAME, D.DEPTNAME, C.CHECKTIME, C.CHECKTYPE
            FROM (CHECKINOUT C 
            LEFT JOIN USERINFO U ON C.USERID = U.USERID)
            LEFT JOIN DEPARTMENTS D ON U.DEFAULTDEPTID = D.DEPTID
            {where_sql}
        """
        cursor.execute(sql, params)
        all_rows = cursor.fetchall()
        conn.close()
        
        _, summary_list = process_attendance_logs(all_rows, EMPLOYEE_DATA, filters, dst_settings)
        
        page = request.args.get('page', 1, type=int)
        per_page = 15
        total_items = len(summary_list)
        total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
        
        start_idx = (page - 1) * per_page
        end_idx = page * per_page
        paginated_summary = summary_list[start_idx:end_idx]
        
        current_dt = datetime.now()
        search_active = any(filters.values())

        return render_template('index.html',
            active_page='summary',
            attendance_summary=paginated_summary,
            page=page,
            total_pages=total_pages,
            filters=filters,
            search_active=search_active,
            departments=departments,
            current_date=current_dt.strftime('%A, %B %d, %Y'),
            now=current_dt.strftime('%I:%M:%S %p')
        )
    except Exception as e:
        return f"<div style='padding:40px; font-family: sans-serif; color: white; background: #000; height: 100vh;'><h2>System Error</h2><p>{e}</p></div>"

@app.route('/employees', methods=['GET', 'POST'])
@admin_required
def manage_employees():
    current_dt = datetime.now()
    employees = load_employees()
    dst_settings = load_dst_settings()

    departments_set = set(emp.get('department') for emp in employees.values() if emp.get('department'))
    departments = sorted(list(departments_set))

    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add':
            badge_id = request.form.get('badge_id').strip()
            name = request.form.get('name').strip()
            time_in = request.form.get('time_in')
            time_out = request.form.get('time_out')
            department = request.form.get('department', 'N/A').strip()
            w_time_in = request.form.get('weekend_time_in', '').strip()
            w_time_out = request.form.get('weekend_time_out', '').strip()
            
            time_in_full = f"{time_in}:00" if len(time_in) == 5 else time_in
            time_out_full = f"{time_out}:00" if len(time_out) == 5 else time_out
            w_time_in_full = f"{w_time_in}:00" if len(w_time_in) == 5 else w_time_in
            w_time_out_full = f"{w_time_out}:00" if len(w_time_out) == 5 else w_time_out
            
            emp_data = {
                "name": name,
                "time_in": time_in_full,
                "time_out": time_out_full,
                "break_parameter": calculate_break_param(time_in_full, time_out_full) or { "start": "13:00:00", "end": "14:00:00" },
                "department": department if department else "N/A",
                "weekend_time_in": w_time_in_full,
                "weekend_time_out": w_time_out_full
            }
            
            w_break = calculate_break_param(w_time_in_full, w_time_out_full)
            if w_break:
                emp_data["weekend_break_parameter"] = w_break
                
            employees[badge_id] = emp_data
            save_employees(employees)
            
        elif action == 'edit':
            badge_id = request.form.get('badge_id')
            new_time_in = request.form.get('time_in')
            new_time_out = request.form.get('time_out')
            new_department = request.form.get('department', 'N/A').strip()
            new_w_time_in = request.form.get('weekend_time_in', '').strip()
            new_w_time_out = request.form.get('weekend_time_out', '').strip()
            effective_date = request.form.get('effective_date', datetime.now().strftime('%Y-%m-%d'))
            
            if badge_id in employees:
                if len(new_time_in) == 5: new_time_in += ":00"
                if len(new_time_out) == 5: new_time_out += ":00"
                if len(new_w_time_in) == 5: new_w_time_in += ":00"
                if len(new_w_time_out) == 5: new_w_time_out += ":00"
                
                if 'schedule_history' not in employees[badge_id]:
                    employees[badge_id]['schedule_history'] = []
                
                employees[badge_id]['schedule_history'].append({
                    "effective_date": effective_date,
                    "time_in": new_time_in,
                    "time_out": new_time_out,
                    "break_parameter": calculate_break_param(new_time_in, new_time_out) or employees[badge_id].get('break_parameter'),
                    "weekend_time_in": new_w_time_in,
                    "weekend_time_out": new_w_time_out,
                    "weekend_break_parameter": calculate_break_param(new_w_time_in, new_w_time_out)
                })
                
                employees[badge_id]['department'] = new_department if new_department else "N/A"
                save_employees(employees)
                
        elif action == 'delete':
            badge_id = request.form.get('badge_id')
            admin_password = request.form.get('admin_password')
            admin_username = session.get('username')
            
            if admin_username in USERS and USERS[admin_username]['password'] == admin_password:
                if badge_id in employees:
                    del employees[badge_id]
                    save_employees(employees)
            else:
                return redirect(url_for('manage_employees', error="Invalid authorization sequence. Deletion aborted."))
                
        return redirect(url_for('manage_employees'))

    error_msg = request.args.get('error')
    search_query = request.args.get('search_name', '').strip().lower()
    
    filtered_employees = {}
    for badge, data in employees.items():
        if search_query in data['name'].lower() or search_query in str(badge):
            filtered_employees[badge] = data

    page = request.args.get('page', 1, type=int)
    per_page = 15
    
    employee_items = list(filtered_employees.items())
    total_employees = len(employee_items)
    total_pages = math.ceil(total_employees / per_page) if total_employees > 0 else 1
    
    start_idx = (page - 1) * per_page
    end_idx = page * per_page
    paginated_employees = dict(employee_items[start_idx:end_idx])

    return render_template('index.html', 
        active_page='employees',
        employees=paginated_employees,
        page=page,
        total_pages=total_pages,
        search_query=search_query,
        error=error_msg,
        dst_settings=dst_settings,
        departments=departments,
        current_date=current_dt.strftime('%A, %B %d, %Y'),
        now=current_dt.strftime('%I:%M:%S %p'))


@app.route('/export')
@admin_required
def export_csv():
    try:
        EMPLOYEE_DATA = load_employees()
        dst_settings = load_dst_settings()
        where_sql, params, filters = get_where_clause(EMPLOYEE_DATA)
        conn = pyodbc.connect(CONN_STR, timeout=5)
        cursor = conn.cursor()
        
        sql = f"""
            SELECT C.USERID, U.BADGENUMBER, U.NAME, D.DEPTNAME, C.CHECKTIME, C.CHECKTYPE
            FROM (CHECKINOUT C 
            LEFT JOIN USERINFO U ON C.USERID = U.USERID)
            LEFT JOIN DEPARTMENTS D ON U.DEFAULTDEPTID = D.DEPTID
            {where_sql}
        """
        cursor.execute(sql, params)
        all_rows = cursor.fetchall()
        
        all_logs, _ = process_attendance_logs(all_rows, EMPLOYEE_DATA, filters, dst_settings)
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Badge ID', 'Name', 'Department', 'Timestamp', 'Log Type', 'Status', 'Remarks'])
        
        for log in all_logs:
            writer.writerow([
                log['BadgeID'], 
                log['Name'], 
                log['Dept'], 
                log['Time'].strftime('%Y-%m-%d %I:%M:%S %p'), 
                log['DisplayType'], 
                log['Remark'],
                log['SpecialRemark']
            ])
            
        conn.close()
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-disposition": f"attachment; filename=Attendance_{datetime.now().strftime('%Y%m%d')}.csv"})
    except Exception as e:
        return f"Export Error: {e}"

@app.route('/export_summary')
@admin_required
def export_summary_csv():
    try:
        EMPLOYEE_DATA = load_employees()
        dst_settings = load_dst_settings()
        where_sql, params, filters = get_where_clause(EMPLOYEE_DATA)
        conn = pyodbc.connect(CONN_STR, timeout=5)
        cursor = conn.cursor()
        
        sql = f"""
            SELECT C.USERID, U.BADGENUMBER, U.NAME, D.DEPTNAME, C.CHECKTIME, C.CHECKTYPE
            FROM (CHECKINOUT C 
            LEFT JOIN USERINFO U ON C.USERID = U.USERID)
            LEFT JOIN DEPARTMENTS D ON U.DEFAULTDEPTID = D.DEPTID
            {where_sql}
        """
        cursor.execute(sql, params)
        all_rows = cursor.fetchall()
        conn.close()
        
        _, summary_list = process_attendance_logs(all_rows, EMPLOYEE_DATA, filters, dst_settings)
            
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow(['Badge ID', 'Employee Name', 'Department', 'Present', 'Absent', 'Dates Absent', 'Late', 'Total Late', 'No Checkout', 'Dates No Checkout', 'Over Break', 'Total Overbreak', 'Overtime', 'Total Overtime', 'Undertime'])
        
        for sum_data in summary_list:
            writer.writerow([
                sum_data['badge'], 
                sum_data['name'], 
                sum_data['dept'], 
                sum_data['present'], 
                sum_data['absent'],
                sum_data.get('absent_dates', ''),
                sum_data['late'], 
                sum_data['formatted_late'],
                sum_data['no_checkout'],
                sum_data.get('dates_no_checkout', ''),
                sum_data['overbreak'], 
                sum_data['formatted_overbreak'],
                sum_data['overtime'],
                sum_data['formatted_overtime'],
                sum_data['undertime']
            ])
            
        return Response(
            output.getvalue(), 
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=Attendance_Summary_{datetime.now().strftime('%Y%m%d')}.csv"}
        )
    except Exception as e:
        return f"Export Error: {e}"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
