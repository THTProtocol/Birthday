
"""Klaudusia's Birthday Scavenger Hunt  -  Urban Hunters Edition
QR-to-task image flow with bonus cards, timing, and competitive mechanics."""

import os, json, uuid, time, re, random
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'klaudusia-bday-' + uuid.uuid4().hex[:12])

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / 'uploads'
STATE_FILE = BASE_DIR / 'state.json'
# ADMIN_PASSWORD kept for reference but no longer required for control panel actions
# (removed to allow easy game restart/reset from /admin without password)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'cakeoclock2026')
SKIP_PENALTY = int(os.environ.get('SKIP_PENALTY', '0'))   # Rules: zero for skipped
BASE_POINTS = int(os.environ.get('BASE_POINTS', '10'))
TIMER_BONUS_FAST = int(os.environ.get('TIMER_BONUS_FAST', '5'))    # <5 min bonus
TIMER_BONUS_MEDIUM = int(os.environ.get('TIMER_BONUS_MEDIUM', '3'))  # <10 min bonus
TIMER_THRESHOLD_FAST = int(os.environ.get('TIMER_THRESHOLD_FAST', '300'))  # seconds
TIMER_THRESHOLD_MEDIUM = int(os.environ.get('TIMER_THRESHOLD_MEDIUM', '600'))

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
        "description": "Steal points from the opposing team.",
        "type": "steal",  # immediate, must specify opponent + amount
        "steal_amount": 10,
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
        'mission_times': {},
        'timing_bonus_earned': 0,
        'blocked_until': None,
        'active_cooldown': None,
    }


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            st = json.load(f)
        # Migrate old team entries that lack new fields
        for name, ts in st.get('teams', {}).items():
            defaults = _default_team(ts.get('color', 'red'))
            for k, v in defaults.items():
                if k not in ts:
                    ts[k] = v
        return st
    return {'teams': {}, 'started': False, 'start_time': None}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


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
    # Check color not already taken
    for tnm, ts in state['teams'].items():
        if ts.get('color') == team_color:
            return jsonify({'error': 'That color is already taken! Choose the other team.'}), 400
    state['teams'][team_name] = _default_team(team_color)
    state['teams'][team_name]['created_at'] = now_iso()
    state['teams'][team_name]['captain'] = member_name
    state['teams'][team_name]['members'] = [{'name': member_name, 'joined_at': now_iso(), 'is_captain': True}]
    # Start timer for the first mission on team creation (so first task has running timer from the start)
    ts = state['teams'][team_name]
    ts['mission_times'] = ts.get('mission_times', {})
    if str(ts['current_mission']) not in ts['mission_times']:
        ts['mission_times'][str(ts['current_mission'])] = now_iso()
    save_state(state)
    return jsonify({'success': True, 'team': team_name, 'color': team_color, 'members': state['teams'][team_name]['members']})


@app.route('/api/team/join', methods=['POST'])
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
def api_unlock():
    """Called when a player scans the QR for their current mission.
    This starts the timer for that mission (if not already started) and confirms unlock.
    The timer starts on scan, not on proof upload."""
    team = request.form.get('team', '').strip()
    mission_id = int(request.form.get('mission_id', '0'))
    state = load_state()
    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400
    ts = state['teams'][team]
    if mission_id == ts.get('current_mission'):
        ts['mission_times'] = ts.get('mission_times', {})
        if str(mission_id) not in ts['mission_times']:
            ts['mission_times'][str(mission_id)] = now_iso()
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
def submit():
    state = load_state()
    team = request.form.get('team', '').strip()
    mission_id = int(request.form.get('mission_id', '0'))

    if team not in state['teams']:
        return jsonify({'error': 'Team not found'}), 400

    ts = state['teams'][team]
    color = ts.get('color', 'red')
    missions = get_missions(color)

    if mission_id != ts['current_mission']:
        return jsonify({'error': f'Complete Mission {ts["current_mission"]} first.'}), 400

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
    if 'photo' in request.files:
        file = request.files['photo']
        if file.filename:
            safe_team = re.sub(r'[^\w\-]', '_', team)[:20]
            ext = Path(file.filename).suffix or '.jpg'
            filename = f"{safe_team}_{mission_id}_{int(time.time())}{ext}"
            (UPLOAD_DIR / filename).write_bytes(file.read())
            photo_url = f"/uploads/{filename}"

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

    # Start timer for the new current mission immediately upon completing the previous
    # (so every task has a running timer + easy buttons like the first one the user liked).
    # The QR scan for a mission "confirms arrival" / unlocks detailed view, but the task is visible with timer from the moment it becomes current.
    if ts['current_mission'] is not None:
        ts['mission_times'][str(ts['current_mission'])] = now_iso()

    # Clear blocks on successful completion
    ts['blocked_until'] = None
    ts['active_cooldown'] = None

    save_state(state)
    # Include next_mission_time so client can start timer immediately (no poll delay)
    next_time = ts['mission_times'].get(str(ts['current_mission'])) if ts['current_mission'] else None
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
def skip():
    state = load_state()
    team = request.form.get('team', '').strip()
    mission_id = int(request.form.get('mission_id', '0'))

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

    ts['skipped'][str(mission_id)] = {
        'timestamp': now_iso(),
        'penalty': penalty,
        'free_skip': free_skip,
    }
    ts['score'] -= penalty  # may be zero (penalty = 0, or free skip)
    ts['current_mission'] = mission_id + 1 if mission_id < len(missions) else None

    # Start timer for the new current mission immediately (consistent with submit, so 2nd/3rd have timer + buttons like the first)
    if ts['current_mission'] is not None:
        ts['mission_times'][str(ts['current_mission'])] = now_iso()

    # Clear blocks on successful skip
    ts['blocked_until'] = None
    ts['active_cooldown'] = None

    save_state(state)
    # Include next_mission_time so client can start timer immediately (no poll delay)
    next_time = ts['mission_times'].get(str(ts['current_mission'])) if ts['current_mission'] else None
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

    state['teams'][team]['bonus_offer'] = {
        'card': card_id,
        'offered_at': now_iso(),
        **BONUS_CARDS[card_id],
    }
    save_state(state)
    return jsonify({'success': True, 'team': team, 'card': card_id})


@app.route('/api/bonus/respond', methods=['POST'])
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
            opp_amount = int(request.form.get('steal_amount', card_def.get('steal_amount', 10)))
            for other_name, other_ts in state['teams'].items():
                if other_name != team and other_ts.get('color') == opponent_color:
                    opponent = other_name
                    break
            if opponent:
                actual_steal = min(opp_amount, state['teams'][opponent]['score'])
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
            ts['active_bonus'] = card_def
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
            ts['active_bonus'] = card_def
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
            ts['active_bonus'] = card_def

        save_state(state)
        return jsonify({
            'success': True, 'action': 'accepted', 'card': card_id,
            'msg': msg if card_type == 'steal' else card_def['name'] + ' is now active!',
        })

    return jsonify({'error': 'Invalid action. Use "accept" or "decline".'}), 400


@app.route('/api/bonus/cancel', methods=['POST'])
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


@app.route('/api/verify/pushups', methods=['POST'])
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

    # Record start time for current mission after unblock
    mid = str(ts['current_mission'])
    ts['mission_times'] = ts.get('mission_times', {})
    ts['mission_times'][mid] = now_iso()

    save_state(state)
    return jsonify({'success': True, 'msg': 'Pushups verified! You are unblocked.'})


# ── Admin ───────────────────────────────────────────────────────

@app.route('/api/admin/reset', methods=['POST'])
def admin_reset():
    # Password requirement removed - easy restart from control panel
    save_state({'teams': {}, 'started': False, 'start_time': None})
    for f in UPLOAD_DIR.iterdir():
        if f.is_file():
            f.unlink()
    return jsonify({'success': True})


@app.route('/api/admin/start', methods=['POST'])
def admin_start():
    # Password requirement removed - easy restart from control panel
    state = load_state()
    state['started'] = True
    state['start_time'] = now_iso()
    # Record mission start times for all teams
    for ts in state['teams'].values():
        ts['mission_times'] = ts.get('mission_times', {})
        ts['mission_times'][str(ts['current_mission'])] = now_iso()
    save_state(state)
    return jsonify({'success': True})


@app.route('/api/admin/bonus', methods=['POST'])
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
