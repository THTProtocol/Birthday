
"""Klaudusia's Birthday Scavenger Hunt  -  Urban Hunters Edition
QR-to-task image flow with bonus cards, timing, and competitive mechanics."""

import os, json, uuid, time, re, random, io, threading, functools
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory

# Cross-thread lock serializing the load -> mutate -> save critical section.
# The app runs with a single gunicorn worker + threads (see birthday-hunt.service),
# so one process-wide lock makes every state mutation atomic and eliminates the
# lost-update race that would otherwise drop scores/scans during the live hunt.
STATE_LOCK = threading.RLock()


def with_state_lock(view):
    """Serialize a mutating endpoint's whole body under STATE_LOCK so its
    read-modify-write of state.json cannot interleave with another request."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        with STATE_LOCK:
            return view(*args, **kwargs)
    return wrapper

# Google Drive upload (optional - requires service account JSON key)
GDRIVE_FOLDER_ID = os.environ.get('GDRIVE_FOLDER_ID', '1EJQGupy6v2fqWdyVXoMurvSMoy0USEkI')
GDRIVE_CREDENTIALS_FILE = os.environ.get('GDRIVE_CREDENTIALS_FILE', '')
_gdrive_service = None

def _get_gdrive_service():
    global _gdrive_service
    if _gdrive_service is not None:
        return _gdrive_service
    if not GDRIVE_CREDENTIALS_FILE or not os.path.exists(GDRIVE_CREDENTIALS_FILE):
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        creds = service_account.Credentials.from_service_account_file(
            GDRIVE_CREDENTIALS_FILE,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        _gdrive_service = build('drive', 'v3', credentials=creds)
        return _gdrive_service
    except Exception as e:
        print(f'Google Drive init failed: {e}')
        return None

def upload_to_gdrive(filepath, filename, team, mission_id):
    """Upload a file to Google Drive folder. Returns drive URL or None."""
    service = _get_gdrive_service()
    if not service:
        return None
    try:
        from googleapiclient.http import MediaFileUpload
        file_metadata = {
            'name': f'{team}_mission{mission_id}_{filename}',
            'parents': [GDRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(filepath, resumable=True)
        uploaded = service.files().create(
            body=file_metadata, media_body=media, fields='id,webViewLink'
        ).execute()
        drive_id = uploaded.get('id')
        drive_url = uploaded.get('webViewLink') or f'https://drive.google.com/file/d/{drive_id}/view'
        return drive_url
    except Exception as e:
        print(f'Google Drive upload failed: {e}')
        return None

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'klaudusia-bday-' + uuid.uuid4().hex[:12])

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
STATE_FILE = BASE_DIR / 'state.json'
# ADMIN_PASSWORD kept for reference but no longer required for control panel actions
# (removed to allow easy game restart/reset from /admin without password)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'cakeoclock2026')
# ── Birthday Edition scoring ────────────────────────────────────
# Completed task = +3, missed (skipped) task = -1, all tasks equal value.
SKIP_PENALTY = int(os.environ.get('SKIP_PENALTY', '1'))   # missed task = -1 point
BASE_POINTS = int(os.environ.get('BASE_POINTS', '3'))     # completed task = +3 points
# Speed bonus (per task, on time since the previous proof/skip):
TIMER_BONUS_FAST = int(os.environ.get('TIMER_BONUS_FAST', '5'))    # under 5 min  -> +5
TIMER_BONUS_MEDIUM = int(os.environ.get('TIMER_BONUS_MEDIUM', '3'))  # 5-10 min   -> +3
TIMER_THRESHOLD_FAST = int(os.environ.get('TIMER_THRESHOLD_FAST', '300'))  # seconds (5 min)
TIMER_THRESHOLD_MEDIUM = int(os.environ.get('TIMER_THRESHOLD_MEDIUM', '600'))  # 10 min
# Bonus card taken but not completed = -2, applied once per failed bonus.
FAILED_BONUS_PENALTY = int(os.environ.get('FAILED_BONUS_PENALTY', '2'))

UPLOAD_DIR.mkdir(exist_ok=True)

# ── Bonus card definitions ──────────────────────────────────────

BONUS_CARDS = {
    "double_strike": {
        "id": "double_strike",
        "name": "DOUBLE STRIKE",
        "description": "The next task yields double the points.",
        "type": "multiplier",  # applied on next /api/submit
        "multiplier": 2,
    },
    "fast_track": {
        "id": "fast_track",
        "name": "FAST TRACK",
        "description": "Skip the next task completely  -  no penalty.",
        "type": "skip_free",  # applied on next /api/skip
    },
    "time_break": {
        "id": "time_break",
        "name": "TIME BREAK",
        "description": "150 pushups must be completed before continuing.",
        "type": "block",  # blocks submissions until pushups verified
        "pushups_required": 150,
    },
    "steal_points": {
        "id": "steal_points",
        "name": "STEAL POINTS",
        "description": "Steal 3 points (one task's worth) from the opposing team.",
        "type": "steal",  # immediate, takes from the other team
        "steal_amount": 3,
    },
    "time_tax": {
        "id": "time_tax",
        "name": "TIME TAX",
        "description": "Enforces a mandatory 5-minute wait before the next move.",
        "type": "cooldown",  # blocks all submissions for 5 minutes
        "cooldown_seconds": 300,
    },
}


def load_missions(filepath):
    with open(filepath) as f:
        return json.load(f)

RED_DATA = load_missions(BASE_DIR / 'missions_red.json')
BLUE_DATA = load_missions(BASE_DIR / 'missions_blue.json')
RED_MISSIONS = RED_DATA['missions']
BLUE_MISSIONS = BLUE_DATA['missions']
HINT_COST = 0  # Hints disabled - call teammates if stuck


def get_missions(color):
    return RED_MISSIONS if color == 'red' else BLUE_MISSIONS


def get_mission_data(color):
    return RED_DATA if color == 'red' else BLUE_DATA


# ── State management ────────────────────────────────────────────

def _default_team(team_color='red'):
    return {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'current_mission': 1,
        'completed': {},
        'skipped': {},
        'score': 0,
        'color': team_color,
        'members': [],           # [{name, joined_at, is_captain}]
        'captain': None,
        'active_bonus': None,
        'bonus_offer': None,
        'bonus_history': [],
        'mission_times': {},   # timer start per mission (set when previous proof uploads)
        'scans': {},           # QR-scan record per mission (set by /api/unlock; gates submission)
        'timing_bonus_earned': 0,
        'blocked_until': None,
        'active_cooldown': None,
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            st = json.load(f)
        # Backfill top-level keys so a pre-existing/older state.json never 500s.
        st.setdefault('teams', {})
        st.setdefault('started', False)
        st.setdefault('start_time', None)
        # Migrate old team entries that lack new fields
        for name, ts in st['teams'].items():
            had_scans = 'scans' in ts   # detect a pre-scans-feature team BEFORE backfilling
            defaults = _default_team(ts.get('color', 'red'))
            for k, v in defaults.items():
                if k not in ts:
                    ts[k] = v
            # ONE-TIME upgrade only: a team created under older code has no 'scans'
            # map, so its current mission was unlocked without a scan record. Treat
            # that current mission as already scanned so the upgrade doesn't
            # soft-lock it. Must NOT run for normal teams (would defeat the scan
            # gate by re-marking each new current mission as scanned on every load).
            if not had_scans:
                cur = ts.get('current_mission')
                if cur is not None:
                    ts['scans'].setdefault(str(cur), now_iso())
                    ts['mission_times'].setdefault(str(cur), now_iso())
        return st
    return {'teams': {}, 'started': False, 'start_time': None}


def save_state(state):
    # Atomic write: write to temp file, then rename to prevent corruption from concurrent writes
    tmp = STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


def get_opponent_color(color):
    return 'blue' if color == 'red' else 'red'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(start_iso):
    """Seconds since start_iso, or None if not set."""
    if not start_iso:
        return None
    try:
        dt = datetime.fromisoformat(start_iso)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


# ── Routes ──────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html',
                         skip_penalty=SKIP_PENALTY,
                         base_points=BASE_POINTS,
                         timer_bonus_fast=TIMER_BONUS_FAST,
                         timer_bonus_medium=TIMER_BONUS_MEDIUM,
                         timer_threshold_fast_sec=TIMER_THRESHOLD_FAST,
                         timer_threshold_medium_sec=TIMER_THRESHOLD_MEDIUM)


@app.route('/unlock/<int:mission_id>')
def unlock(mission_id):
    color = request.args.get('c', 'red')
    team_name = request.args.get('t', '')
    missions = get_missions(color)
    mission = next((m for m in missions if m['id'] == mission_id), None)
    if not mission:
        return render_template('unlock_result.html', success=False, message="Mission not found.")
    next_mission = next((m for m in missions if m['id'] == mission_id + 1), None)
    next_hint = mission.get('next_hint')
    if not next_hint and next_mission:
        next_hint = 'Find the next QR code at the location described after the previous task!'
    elif not next_hint:
        next_hint = None
    return render_template('unlock.html', mission=mission, color=color,
                         team_name=team_name,
                         base_points=BASE_POINTS, skip_penalty=SKIP_PENALTY,
                         next_hint=next_hint, is_last=next_mission is None,
                         timer_bonus_fast=TIMER_BONUS_FAST,
                         timer_bonus_medium=TIMER_BONUS_MEDIUM)


# ── API ─────────────────────────────────────────────────────────

@app.route('/api/missions')
def api_missions():
    team = request.args.get('team', '')
    if team:
        state = load_state()
        if team in state['teams']:
            color = state['teams'][team].get('color', 'red')
            return jsonify(get_missions(color))
    return jsonify(RED_MISSIONS)


@app.route('/api/team/create', methods=['POST'])
@with_state_lock
def team_create():
    team_name = request.form.get('team', '').strip()
    member_name = request.form.get('member', '').strip()
    team_color = request.form.get('color', 'red').strip()
    if not team_name or len(team_name) > 30:
        return jsonify({'error': 'Invalid team name'}), 400
    if not member_name or len(member_name) > 24:
        return jsonify({'error': 'Enter your name'}), 400
    if team_color not in ('red', 'blue'):
        team_color = 'red'
    state = load_state()
    # A duplicate name would overwrite the existing team (name is the dict key).
    if team_name in state['teams']:
        return jsonify({'error': 'A team with that name already exists! Pick another name.'}), 400
    # Check color not already taken
    for tnm, ts in state['teams'].items():
        if ts.get('color') == team_color:
            return jsonify({'error': 'That color is already taken! Choose the other team.'}), 400
    state['teams'][team_name] = _default_team(team_color)
    state['teams'][team_name]['created_at'] = now_iso()
    state['teams'][team_name]['captain'] = member_name
    state['teams'][team_name]['members'] = [{'name': member_name, 'joined_at': now_iso(), 'is_captain': True}]
    # Start timer for the first mission on team creation (so first task has running timer
    # from the start) and mark it scanned - mission 1 is handed out, no QR hunt needed.
    ts = state['teams'][team_name]
    ts['mission_times'] = ts.get('mission_times', {})
    ts['scans'] = ts.get('scans', {})
    if str(ts['current_mission']) not in ts['mission_times']:
        ts['mission_times'][str(ts['current_mission'])] = now_iso()
    ts['scans'][str(ts['current_mission'])] = now_iso()
    save_state(state)
    return jsonify({'success': True, 'team': team_name, 'color': team_color, 'members': state['teams'][team_name]['members']})


@app.route('/api/team/join', methods=['POST'])
@with_state_lock
def team_join():
    team_name = request.form.get('team', '').strip()
    member_name = request.form.get('member', '').strip()
    if not team_name or not member_name or len(member_name) > 24:
        return jsonify({'error': 'Invalid name'}), 400
    state = load_state()
    if team_name not in state['teams']:
        return jsonify({'error': 'Team not found'}), 404
    ts = state['teams'][team_name]
    # Prevent duplicate name
    for m in ts.get('members', []):
        if m['name'].lower() == member_name.lower():
            return jsonify({'error': 'That name is already in this team!'}), 400
    ts['members'].append({'name': member_name, 'joined_at': now_iso(), 'is_captain': False})
    # Ensure timer is set for current mission on join (for first mission mainly)
    ts['mission_times'] = ts.get('mission_times', {})
    if str(ts['current_mission']) not in ts['mission_times']:
        ts['mission_times'][str(ts['current_mission'])] = now_iso()
    save_state(state)
    return jsonify({'success': True, 'team': team_name, 'color': ts.get('color', 'red'), 'members': ts['members']})

@app.route('/api/unlock', methods=['POST'])
@with_state_lock
def api_unlock():
    """Called when a player scans the QR for their current mission. Records the
    scan that unlocks the task. The mission timer itself starts when the previous
    proof uploads, NOT on scan."""
    team = request.form.get('team', '').strip()
    try:
        mission_id = int(request.form.get('mission_id', '0'))
    except (ValueError, TypeError):
        return jsonify({'error': 'Bad mission id'}), 400
    qr_color = request.form.get('color', '').strip()
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    ts = state['teams'][team]
    # A QR must match the team's color. Reject if the color is missing or wrong -
    # the two teams share mission ids 1-10, so this is the only cross-team barrier.
    if qr_color != ts.get('color', 'red'):
        return jsonify({'error': "That QR belongs to the other team! Find your own team's QR."}), 400
    if mission_id == ts.get('current_mission'):
        ts['scans'] = ts.get('scans', {})
        ts['mission_times'] = ts.get('mission_times', {})
        changed = False
        if str(mission_id) not in ts['scans']:
            ts['scans'][str(mission_id)] = now_iso()
            changed = True
        # Timer normally starts when the previous proof uploads; this is only a
        # fallback so a missing record never leaves a mission without a timer.
        if str(mission_id) not in ts['mission_times']:
            ts['mission_times'][str(mission_id)] = now_iso()
            changed = True
        if changed:
            save_state(state)
    return jsonify({'success': True})


@app.route('/api/team/list')
def team_list():
    state = load_state()
    out = {}
    for name, ts in state['teams'].items():
        out[name] = {
            'name': name,
            'color': ts.get('color', 'red'),
            'member_count': len(ts.get('members', [])),
            'captain': ts.get('captain'),
            'score': ts.get('score', 0),
        }
    return jsonify(out)


@app.route('/api/submit', methods=['POST'])
@with_state_lock
def submit():
    state = load_state()
    team = request.form.get('team', '').strip()
    try:
        mission_id = int(request.form.get('mission_id', '0'))
    except (ValueError, TypeError):
        return jsonify({'error': 'Bad mission id'}), 400

    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400

    ts = state['teams'][team]
    color = ts.get('color', 'red')
    missions = get_missions(color)

    if mission_id != ts['current_mission']:
        return jsonify({'error': f'Complete Mission {ts["current_mission"]} first.'}), 400

    # ── QR scan requirement (server-side) ─────────────────────
    # /api/unlock records the scan in `scans`; without that record the QR was
    # never scanned, so the mission cannot be completed.
    if str(mission_id) not in ts.get('scans', {}):
        return jsonify({'error': 'Scan the QR code at this location first to unlock this mission.'}), 400

    # ── Block checks ──────────────────────────────────────────
    if ts.get('blocked_until'):
        block_end = ts['blocked_until']
        try:
            end_dt = datetime.fromisoformat(block_end)
            if datetime.now(timezone.utc) < end_dt:
                remaining = int((end_dt - datetime.now(timezone.utc)).total_seconds())
                return jsonify({'error': f'Blocked! {ts.get("active_cooldown", "Cooldown")} - {remaining}s remaining.'}), 400
        except (ValueError, TypeError):
            pass

    # TIME BREAK block (pushups)
    active_b = ts.get('active_bonus')
    if active_b and active_b.get('type') == 'block':
        pushups = active_b.get('pushups_required', 150)
        return jsonify({'error': f'TIME BREAK! Complete {pushups} pushups first.'}), 400

    # ── Photo requirement ─────────────────────────────────────
    photo_url = None
    gdrive_args = None
    if 'photo' in request.files:
        file = request.files['photo']
        if file.filename:
            safe_team = re.sub(r'[^\w\-]', '_', team)[:20]
            ext = Path(file.filename).suffix or '.jpg'
            filename = f"{safe_team}_{mission_id}_{int(time.time())}{ext}"
            filepath = UPLOAD_DIR / filename
            filepath.write_bytes(file.read())   # local write is fast, fine under lock
            photo_url = f"/uploads/{filename}"
            # Defer the slow Google Drive upload to a background thread so it does
            # not hold STATE_LOCK (best-effort backup; its URL is not stored).
            gdrive_args = (str(filepath), filename, team, mission_id)

    if not photo_url:
        return jsonify({'error': 'Photo proof is required to complete this mission.'}), 400

    # ── Timing bonus ──────────────────────────────────────────
    start_ts = ts.get('mission_times', {}).get(str(mission_id))
    elapsed = elapsed_seconds(start_ts) if start_ts else None
    timing_bonus = 0
    if elapsed is not None:
        if elapsed <= TIMER_THRESHOLD_FAST:
            timing_bonus = TIMER_BONUS_FAST
        elif elapsed <= TIMER_THRESHOLD_MEDIUM:
            timing_bonus = TIMER_BONUS_MEDIUM

    # ── Active bonus card check ───────────────────────────────
    active = ts.get('active_bonus')
    multiplier = 1
    bonus_msg = ''

    if active and active.get('type') == 'multiplier':
        multiplier = int(active.get('multiplier', 1))
        bonus_msg = f' (x{multiplier} DOUBLE STRIKE!)'
        ts['bonus_history'].append({
            'card': 'double_strike',
            'mission_id': mission_id,
            'applied_at': now_iso(),
            'multiplier': multiplier,
        })
        ts['active_bonus'] = None

    earned = (BASE_POINTS * multiplier) + timing_bonus

    ts['completed'][str(mission_id)] = {
        'timestamp': now_iso(),
        'photo_url': photo_url,
        'points': earned,
        'base_points': BASE_POINTS,
        'multiplier': multiplier,
        'timing_bonus': timing_bonus,
        'elapsed_seconds': elapsed,
    }
    ts['score'] += earned
    if timing_bonus:
        ts['timing_bonus_earned'] = ts.get('timing_bonus_earned', 0) + timing_bonus
    ts['current_mission'] = mission_id + 1 if mission_id < len(missions) else None

    # The next mission's TIMER starts right now - travel and QR hunting count
    # against the speed bonus. The task itself stays locked until its QR is
    # scanned (`scans` record via /api/unlock).
    next_time = None
    if ts['current_mission'] is not None:
        next_time = now_iso()
        ts['mission_times'][str(ts['current_mission'])] = next_time

    # Clear blocks/cooldown on successful completion, and spend any single-use
    # card that was taken but not used by THIS action so it cannot leak to a
    # later mission (a multiplier, if present, was already consumed above).
    ts['blocked_until'] = None
    ts['active_cooldown'] = None
    leftover = ts.get('active_bonus')
    if leftover and leftover.get('type') in ('skip_free', 'cooldown'):
        ts['active_bonus'] = None

    save_state(state)
    # Best-effort Drive backup, off the lock and off the response path.
    if gdrive_args:
        threading.Thread(target=upload_to_gdrive, args=gdrive_args, daemon=True).start()
    return jsonify({
        'correct': True,
        'score': ts['score'],
        'earned': earned,
        'timing_bonus': timing_bonus,
        'elapsed_seconds': elapsed,
        'bonus_msg': bonus_msg,
        'next_mission': ts['current_mission'],
        'next_mission_time': next_time,
        'finished': ts['current_mission'] is None,
    })


@app.route('/api/skip', methods=['POST'])
@with_state_lock
def skip():
    state = load_state()
    team = request.form.get('team', '').strip()
    try:
        mission_id = int(request.form.get('mission_id', '0'))
    except (ValueError, TypeError):
        return jsonify({'error': 'Bad mission id'}), 400

    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400

    ts = state['teams'][team]
    color = ts.get('color', 'red')
    missions = get_missions(color)

    if mission_id != ts['current_mission']:
        return jsonify({'error': f'Can only skip #{ts["current_mission"]}'}), 400

    # ── Block checks ──────────────────────────────────────────
    if ts.get('blocked_until'):
        block_end = ts['blocked_until']
        try:
            end_dt = datetime.fromisoformat(block_end)
            if datetime.now(timezone.utc) < end_dt:
                remaining = int((end_dt - datetime.now(timezone.utc)).total_seconds())
                return jsonify({'error': f'Blocked! {ts.get("active_cooldown", "Cooldown")} - {remaining}s remaining.'}), 400
        except (ValueError, TypeError):
            pass

    # TIME BREAK block (pushups)
    active_b = ts.get('active_bonus')
    if active_b and active_b.get('type') == 'block':
        pushups = active_b.get('pushups_required', 150)
        return jsonify({'error': f'TIME BREAK! Complete {pushups} pushups first.'}), 400

    # ── Active bonus check ────────────────────────────────────
    active = ts.get('active_bonus')
    if active and active.get('type') == 'skip_free':
        # FAST TRACK  -  free skip
        penalty = 0
        free_skip = True
        ts['bonus_history'].append({
            'card': 'fast_track',
            'mission_id': mission_id,
            'applied_at': now_iso(),
        })
        ts['active_bonus'] = None
    else:
        penalty = SKIP_PENALTY
        free_skip = False
        # Spend any leftover single-use card on this forward action so a taken
        # DOUBLE STRIKE / cooldown card cannot leak to a later mission.
        if active and active.get('type') in ('multiplier', 'cooldown'):
            ts['active_bonus'] = None

    ts['skipped'][str(mission_id)] = {
        'timestamp': now_iso(),
        'penalty': penalty,
        'free_skip': free_skip,
    }
    ts['score'] -= penalty  # may be zero (penalty = 0, or free skip)
    ts['current_mission'] = mission_id + 1 if mission_id < len(missions) else None

    # Skipping starts the next mission's timer too - the clock never pauses.
    # The task itself stays locked until its QR is scanned.
    next_time = None
    if ts['current_mission'] is not None:
        next_time = now_iso()
        ts['mission_times'][str(ts['current_mission'])] = next_time

    # Clear blocks on successful skip
    ts['blocked_until'] = None
    ts['active_cooldown'] = None

    save_state(state)
    return jsonify({
        'skipped': True,
        'penalty': penalty,
        'free_skip': free_skip,
        'score': ts['score'],
        'next_mission': ts['current_mission'],
        'next_mission_time': next_time,
        'finished': ts['current_mission'] is None,
    })


@app.route('/api/hint', methods=['POST'])
def buy_hint():
    # Hints are disabled for this hunt. Call your teammates if stuck.
    return jsonify({'error': 'Hints are disabled. Call your teammates if you are stuck!'}), 400


@app.route('/api/state')
def api_state():
    state = load_state()
    teams_summary = {}
    for name, ts in state['teams'].items():
        teams_summary[name] = {
            'current_mission': ts['current_mission'],
            'score': ts['score'],
            'completed': len(ts['completed']),
            'skipped': len(ts['skipped']),
            'color': ts.get('color', 'red'),
            'members': ts.get('members', []),
            'captain': ts.get('captain'),
            'active_bonus': ts.get('active_bonus'),
            'bonus_offer': ts.get('bonus_offer'),
            'timing_bonus_earned': ts.get('timing_bonus_earned', 0),
            'blocked_until': ts.get('blocked_until'),
            'active_cooldown': ts.get('active_cooldown'),
        }
    return jsonify({
        'teams': teams_summary,
        'started': state['started'],
        'start_time': state['start_time'],
        'total_missions': len(RED_MISSIONS),
        'bonus_cards_available': list(BONUS_CARDS.keys()),
    })


@app.route('/api/team/<team_name>')
def api_team(team_name):
    state = load_state()
    if team_name not in state['teams']:
        return jsonify({'error': 'Team not found'}), 404
    return jsonify(state['teams'][team_name])


# ── Bonus card endpoints ────────────────────────────────────────

@app.route('/api/bonus/offer', methods=['POST'])
@with_state_lock
def bonus_offer():
    """Admin offers a bonus card to a team. Team must accept or decline."""
    # Password requirement removed for easy control panel access
    team = request.form.get('team', '').strip()
    card_id = request.form.get('card', '').strip()

    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    if card_id not in BONUS_CARDS:
        return jsonify({'error': f'Unknown card. Available: {list(BONUS_CARDS.keys())}'}), 400

    # Don't let a new offer erase an unresolved obligation. Resolve the active
    # card first (Done / Failed / Clear in the admin panel) before offering another.
    if state['teams'][team].get('active_bonus'):
        return jsonify({'error': 'Team already has an active bonus. Resolve it first (Done / Failed -2).'}), 400

    state['teams'][team]['bonus_offer'] = {
        'card': card_id,
        'offered_at': now_iso(),
        **BONUS_CARDS[card_id],
    }
    save_state(state)
    return jsonify({'success': True, 'team': team, 'card': card_id})


@app.route('/api/bonus/respond', methods=['POST'])
@with_state_lock
def bonus_respond():
    """Team accepts or declines a pending bonus card offer."""
    team = request.form.get('team', '').strip()
    action = request.form.get('action', '').strip()  # 'accept' or 'decline'

    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400

    ts = state['teams'][team]
    offer = ts.get('bonus_offer')

    if not offer:
        return jsonify({'error': 'No pending bonus card offer.'}), 400

    card_id = offer['card']
    card_def = BONUS_CARDS.get(card_id, {})

    if action == 'decline':
        ts['bonus_offer'] = None
        save_state(state)
        return jsonify({'success': True, 'action': 'declined', 'card': card_id})

    if action == 'accept':
        ts['bonus_offer'] = None

        card_type = card_def.get('type', '')
        if card_type == 'steal':
            # STEAL POINTS  -  immediate, steal from opponent
            opponent = None
            opponent_color = get_opponent_color(ts.get('color', 'red'))
            opp_amount = int(request.form.get('steal_amount', card_def.get('steal_amount', 3)))
            for other_name, other_ts in state['teams'].items():
                if other_name != team and other_ts.get('color') == opponent_color:
                    opponent = other_name
                    break
            if opponent:
                actual_steal = min(opp_amount, state['teams'][opponent].get('score', 0))
                state['teams'][opponent]['score'] -= actual_steal
                ts['score'] += actual_steal
                ts['bonus_history'].append({
                    'card': card_id,
                    'applied_at': now_iso(),
                    'stole_from': opponent,
                    'amount': actual_steal,
                })
                msg = f'Stole {actual_steal} pts from {opponent}!'
            else:
                msg = 'No opponent to steal from.'
        elif card_type == 'block':
            # TIME BREAK  -  set pushup requirement
            ts['active_bonus'] = dict(card_def)   # copy so teams don't share the template dict
            ts['blocked_until'] = None
            ts['active_cooldown'] = f'TIME BREAK: {card_def["pushups_required"]} pushups'
            ts['bonus_history'].append({
                'card': card_id,
                'applied_at': now_iso(),
            })
            msg = 'TIME BREAK active! Complete pushups to unlock.'
        elif card_type == 'cooldown':
            # TIME TAX  -  5-minute block
            cooldown_end = datetime.now(timezone.utc)
            cooldown_sec = card_def.get('cooldown_seconds', 300)
            from datetime import timedelta
            cooldown_end = cooldown_end + timedelta(seconds=cooldown_sec)
            ts['blocked_until'] = cooldown_end.isoformat()
            ts['active_cooldown'] = 'TIME TAX  -  5 min wait'
            ts['active_bonus'] = dict(card_def)   # copy so teams don't share the template dict
            ts['bonus_history'].append({
                'card': card_id,
                'applied_at': now_iso(),
                'cooldown_seconds': cooldown_sec,
            })
            save_state(state)
            return jsonify({
                'success': True, 'action': 'accepted', 'card': card_id,
                'type': 'cooldown',
                'blocked_until': ts['blocked_until'],
                'cooldown_seconds': cooldown_sec,
            })
        else:
            # All other cards: store as active bonus, applied on next action
            ts['active_bonus'] = dict(card_def)   # copy so teams don't share the template dict

        save_state(state)
        return jsonify({
            'success': True, 'action': 'accepted', 'card': card_id,
            'msg': msg if card_type == 'steal' else card_def['name'] + ' is now active!',
        })

    return jsonify({'error': 'Invalid action. Use "accept" or "decline".'}), 400


@app.route('/api/bonus/cancel', methods=['POST'])
@with_state_lock
def bonus_cancel():
    """Admin cancels a pending bonus offer for a team."""
    # Password requirement removed for easy control panel access
    team = request.form.get('team', '').strip()
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    state['teams'][team]['bonus_offer'] = None
    save_state(state)
    return jsonify({'success': True})


@app.route('/api/bonus/fail', methods=['POST'])
@with_state_lock
def bonus_fail():
    """A taken bonus card was NOT completed. Applies the failed-bonus penalty
    (-2) once, then clears the active bonus and any block/cooldown it created so
    the team can keep playing. Gamemaster-triggered from the admin panel.
    'Once per failed bonus' is guaranteed: clearing active_bonus means there is
    nothing left to fail until the team takes another card."""
    team = request.form.get('team', '').strip()
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    ts = state['teams'][team]
    active = ts.get('active_bonus')
    if not active:
        return jsonify({'error': 'No taken bonus card to mark as failed.'}), 400
    # TIME TAX (cooldown) is a pure time penalty - there is nothing to "complete",
    # so it cannot be failed. Clear it without an extra -2 (use Clear instead).
    if active.get('type') == 'cooldown':
        return jsonify({'error': 'TIME TAX is a wait, not a task to complete. Use Clear instead.'}), 400
    penalty = FAILED_BONUS_PENALTY
    ts['score'] -= penalty
    ts['bonus_history'].append({
        'card': active.get('id', 'unknown'),
        'failed_at': now_iso(),
        'penalty': penalty,
    })
    ts['active_bonus'] = None
    ts['blocked_until'] = None
    ts['active_cooldown'] = None
    save_state(state)
    return jsonify({'success': True, 'penalty': penalty, 'score': ts['score']})


@app.route('/api/verify/pushups', methods=['POST'])
@with_state_lock
def verify_pushups():
    """Team self-verifies that pushups are done."""
    team = request.form.get('team', '').strip()
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400

    ts = state['teams'][team]
    active = ts.get('active_bonus')

    if not active or active.get('type') != 'block':
        return jsonify({'error': 'No TIME BREAK is active.'}), 400

    ts['blocked_until'] = None
    ts['active_cooldown'] = None
    ts['active_bonus'] = None
    ts['bonus_history'].append({
        'card': 'time_break',
        'verified_at': now_iso(),
        'pushups': active.get('pushups_required', 150),
    })

    # Record start time for current mission after unblock (skip if finished)
    if ts.get('current_mission') is not None:
        mid = str(ts['current_mission'])
        ts['mission_times'] = ts.get('mission_times', {})
        ts['mission_times'][mid] = now_iso()

    save_state(state)
    return jsonify({'success': True, 'msg': 'Pushups verified! You are unblocked.'})


# ── Admin ───────────────────────────────────────────────────────

@app.route('/api/admin/reset', methods=['POST'])
@with_state_lock
def admin_reset():
    # Password requirement removed - easy restart from control panel
    save_state({'teams': {}, 'started': False, 'start_time': None})
    for f in UPLOAD_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass
    return jsonify({'success': True})


@app.route('/api/admin/start', methods=['POST'])
@with_state_lock
def admin_start():
    # Password requirement removed - easy restart from control panel
    state = load_state()
    state['started'] = True
    state['start_time'] = now_iso()
    # Record mission start times for all teams (and unlock their current
    # mission - the game master is handing out the first task).
    for ts in state['teams'].values():
        ts['mission_times'] = ts.get('mission_times', {})
        ts['scans'] = ts.get('scans', {})
        ts['mission_times'][str(ts['current_mission'])] = now_iso()
        ts['scans'][str(ts['current_mission'])] = now_iso()
    save_state(state)
    return jsonify({'success': True})


@app.route('/api/admin/bonus', methods=['POST'])
@with_state_lock
def admin_bonus():
    # Password requirement removed - easy control panel access
    team = request.form.get('team', '').strip()
    pts = int(request.form.get('points', 0))
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    state['teams'][team]['score'] += pts
    save_state(state)
    return jsonify({'success': True, 'score': state['teams'][team]['score']})


@app.route('/api/admin/state')
def admin_state():
    # Password requirement removed - easy control panel access (no ?password needed)
    return jsonify(load_state())


@app.route('/api/admin/clear-block', methods=['POST'])
@with_state_lock
def admin_clear_block():
    """Admin forcibly clears a team's block (TIME TAX / TIME BREAK)."""
    # Password requirement removed - easy control panel access
    team = request.form.get('team', '').strip()
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    state['teams'][team]['blocked_until'] = None
    state['teams'][team]['active_cooldown'] = None
    state['teams'][team]['active_bonus'] = None
    save_state(state)
    return jsonify({'success': True})


# ── Editor routes ───────────────────────────────────────────────

@app.route('/admin')
def admin():
    return render_template('admin.html',
                         bonus_cards=BONUS_CARDS,
                         title="Klaudusia's Birthday Hunt")


@app.route('/editor')
def editor():
    return render_template('editor.html')


@app.route('/api/editor/load', methods=['POST'])
def editor_load():
    # Password requirement removed for full unrestricted access to all 20 missions
    color = request.form.get('color', 'red')
    # Always read fresh from disk so editor sees the latest full set of missions
    filepath = BASE_DIR / ('missions_red.json' if color == 'red' else 'missions_blue.json')
    data = load_missions(filepath)
    return jsonify({'missions': data['missions'], 'title': data.get('title', ''), 'color': color})


@app.route('/api/editor/save', methods=['POST'])
@with_state_lock
def editor_save():
    # Password requirement removed for full unrestricted access
    color = request.form.get('color', 'red')
    data = json.loads(request.form.get('missions', '[]'))
    if not data:
        return jsonify({'error': 'No data'}), 400
    filepath = BASE_DIR / ('missions_red.json' if color == 'red' else 'missions_blue.json')
    current = load_missions(filepath)
    current['missions'] = data
    with open(filepath, 'w') as f:
        json.dump(current, f, indent=2)
    global RED_MISSIONS, BLUE_MISSIONS, RED_DATA, BLUE_DATA
    if color == 'red':
        RED_MISSIONS = data
        RED_DATA = current
    else:
        BLUE_MISSIONS = data
        BLUE_DATA = current
    return jsonify({'success': True, 'count': len(data), 'color': color})


@app.route('/api/editor/upload', methods=['POST'])
def editor_upload():
    # Password requirement removed for full access
    file = request.files.get('image')
    if not file or not file.filename:
        return jsonify({'error': 'No image file'}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        return jsonify({'error': 'Invalid file type. Use jpg/png/gif/webp'}), 400
    img_dir = BASE_DIR / 'static' / 'images'
    img_dir.mkdir(exist_ok=True)
    filename = f"mission_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
    (img_dir / filename).write_bytes(file.read())
    return jsonify({'success': True, 'url': f'/static/images/{filename}'})


@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)


@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(str(BASE_DIR / 'static'), filename)

@app.route('/qr-codes-v2/<path:filename>')
def qr_codes_v2(filename):
    return send_from_directory(str(BASE_DIR / 'qr-codes-v2'), filename)

@app.route('/qr')
def qr_page():
    import json
    red_m = json.load(open(BASE_DIR / 'missions_red.json'))['missions']
    blue_m = json.load(open(BASE_DIR / 'missions_blue.json'))['missions']
    html = '<h1>QR Codes - Print and Hide</h1>'
    html += '<p>Run in terminal: <code>python3 generate_qr.py http://YOUR_IP:8080</code> (replace YOUR_IP with 192.168.8.101 or the tunnel URL)</p>'
    html += '<h2>Pink/Red Team (10 QRs)</h2><ul>'
    for m in red_m:
        html += f'<li><a href="/qr-codes-v2/red-mission-{m["id"]:02d}.png" target="_blank">Red {m["id"]}: {m["title"]} - {m["location"]}</a></li>'
    html += '</ul>'
    html += '<h2>Blue Team (10 QRs)</h2><ul>'
    for m in blue_m:
        html += f'<li><a href="/qr-codes-v2/blue-mission-{m["id"]:02d}.png" target="_blank">Blue {m["id"]}: {m["title"]} - {m["location"]}</a></li>'
    html += '</ul>'
    html += '<p>Decoys are also in qr-codes-v2/.</p>'
    return html

# Serve mission JSONs directly for the editor to load without password barrier
@app.route('/missions/red.json')
def missions_red_json():
    return send_from_directory(str(BASE_DIR), 'missions_red.json')

@app.route('/missions/blue.json')
def missions_blue_json():
    return send_from_directory(str(BASE_DIR), 'missions_blue.json')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
