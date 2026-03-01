from flask import Flask, render_template_string, redirect, url_for, send_file, jsonify, request
import os, json, csv, time, threading
from datetime import datetime
from smbus2 import SMBus, i2c_msg

app = Flask(__name__)

# ---------------- Globals ----------------
USER_NAME = "FastPaw Operator"
teams = {}
current_teams = ["Left", "Right"]
team_splits = {"Left": [None]*10, "Right": [None]*10}
finish_times = {"Left": [None]*10, "Right": [None]*10}
dog_index = {"Left": 0, "Right": 0}
lane_red = {"Left": False, "Right": False}

dog_start_times = {"Left": [None]*10, "Right": [None]*10}

race_running = False
judge_ready_flag = False
green_time = None
race_number = 1
heat_number = 1
last_status = ""
start_sequence_begin = None

last_press_time = {10: 0, 11: 0, 12: 0, 13: 0}
DEBOUNCE_MS = 50
IGNORE_AFTER_MS = 3000
CHECK_INTERVAL = 0.01
lane_delay_until = {"Left": 0.0, "Right": 0.0}
ENTER_DELAY = 2.0  # seconds

DATA_DIR = "data"
RACE_TABLE = os.path.join(DATA_DIR, "UFLI_races.csv")
LIVE_STATE = os.path.join(DATA_DIR, "live_state.json")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- Pin map ----------------
PIN_LEFT_RED    = 0
PIN_RIGHT_RED   = 1
PIN_BLUE_TOP    = 2
PIN_YELLOW_TOP  = 3
PIN_YELLOW_BOT  = 4
PIN_LEFT_GREEN  = 5
PIN_RIGHT_GREEN = 6
PIN_READY_1     = 7   # Judge Ready indicator
PIN_READY_2     = 8   # Left lane aligned
PIN_READY_3     = 9   # Right lane aligned
PIN_LEFT_ENTER  = 10
PIN_LEFT_EXIT   = 11
PIN_RIGHT_ENTER = 12
PIN_RIGHT_EXIT  = 13

# ---------------- PCF8575 ----------------
class PCF8575:
    """Active-high outputs (0–9) and active-low inputs (10–13)."""
    def __init__(self, bus=5, addr=0x20):
        self.bus = SMBus(bus)
        self.addr = addr
        self.state = 0xFC00
        self._write_state()
        print(f"✅ PCF8575 ready on /dev/i2c-{bus}, addr 0x{addr:02X}")

    def _write_state(self):
        low = self.state & 0xFF
        high = (self.state >> 8) & 0xFF
        self.bus.i2c_rdwr(i2c_msg.write(self.addr, [low, high]))

    def set_pin(self, pin, on):
        if pin >= 10:
            return
        if on:
            self.state |= (1 << pin)
        else:
            self.state &= ~(1 << pin)
        self._write_state()

    def read_state(self):
        read = i2c_msg.read(self.addr, 2)
        self.bus.i2c_rdwr(read)
        data = list(read)
        return data[0] | (data[1] << 8)


## Dummy Expander for Troubleshooting
class DummyExpander:
    def set_pin(self, pin, on):  # ignore LED writes
        print(f"(Sim) set_pin({pin}, {on})")
    def read_state(self):       # no inputs; always high
        return 0xFFFF


# Use dummy expander for development
#expander = DummyExpander()
# For real hardware, comment above and uncomment below:
expander = PCF8575(bus=5, addr=0x20)

# Global expander and tracking flags
dog_entered = {"Left": False, "Right": False}
dog_exited  = {"Left": False, "Right": False}

# ---------------- Warm-up timer (server-side) ----------------
warmup_active = False
warmup_end_ts = None
warmup_lock = threading.Lock()

def warmup_remaining_seconds():
    if not warmup_active or warmup_end_ts is None:
        return 0
    rem = int(round(warmup_end_ts - time.time()))
    return rem if rem > 0 else 0

def warmup_set(duration_seconds):
    global warmup_active, warmup_end_ts, last_status
    with warmup_lock:
        warmup_active = True
        warmup_end_ts = time.time() + int(duration_seconds)
        last_status = f"⏱️ Warm-Up started for {int(duration_seconds)}s"

    threading.Thread(target=save_live_state, daemon=True).start()

def warmup_cancel():
    global warmup_active, warmup_end_ts, last_status
    with warmup_lock:
        warmup_active = False
        warmup_end_ts = None
        last_status = "⏹️ Warm-Up cancelled"

    save_live_state()

def alignment_poller():
    """Continuously update alignment lights when race is NOT running and judge not ready."""
    last_left = None
    last_right = None

    while True:
        time.sleep(0.5)  # Check every 500ms

        # Only update alignment lights if race is not running AND judge is not ready
        if not race_running and not judge_ready_flag:
            try:
                left_ok = lane_aligned("Left")
                right_ok = lane_aligned("Right")

                # Only update if state changed
                if left_ok != last_left or right_ok != last_right:
                    expander.set_pin(PIN_READY_2, left_ok)
                    expander.set_pin(PIN_READY_3, right_ok)
                    last_left = left_ok
                    last_right = right_ok
                    print(f"Alignment updated: Left={left_ok}, Right={right_ok}")
            except Exception as e:
                print("Alignment poller error:", e)
        else:
            # Reset tracking when judge ready or race is running
            last_left = None
            last_right = None

def warmup_housekeeper():
    global warmup_active, warmup_end_ts, last_status

    while True:
        time.sleep(0.25)   # 4 updates per second

        # If warmup is not active, skip
        if not warmup_active:
            continue

        # If warmup timer is broken or unset, skip
        if warmup_end_ts is None:
            continue

        # ALWAYS update state so display sees countdown
        save_live_state()

        # Check if warmup expired
        if time.time() >= warmup_end_ts:
            with warmup_lock:
                warmup_active = False
                warmup_end_ts = None
                last_status = "✅ Warm-Up complete"

            save_live_state()


# ---------------- Helpers ----------------
def next_race_number():
    if not os.path.exists(RACE_TABLE):
        return 1
    with open(RACE_TABLE, newline="") as f:
        rows = list(csv.DictReader(f))
    nums = [int(r["Racenumber"]) for r in rows if r.get("Racenumber")]
    return max(nums, default=0) + 1

def winner_light_for_2_minutes(winning_lane):
    """
    Turn on the winner's green LED for 2 minutes, then turn it off.
    """

    if winning_lane == "Left":
        pin = PIN_LEFT_GREEN
    else:
        pin = PIN_RIGHT_GREEN

    def worker():
        # Turn ON
        expander.set_pin(pin, True)
        print(f"Winner light ON for {winning_lane} lane")

        # stay on 2 minutes
        time.sleep(120)

        # Turn OFF
        expander.set_pin(pin, False)
        print(f"Winner light OFF for {winning_lane} lane")

    threading.Thread(target=worker, daemon=True).start()

def flash_false_start_light(red_pin):
    """Turn on the false start red light for 2 seconds, then turn it off."""
    def worker():
        expander.set_pin(red_pin, True)
        time.sleep(2)
        expander.set_pin(red_pin, False)

    threading.Thread(target=worker, daemon=True).start()

def read_race_table():
    if not os.path.exists(RACE_TABLE):
        return []
    with open(RACE_TABLE, newline="") as f:
        return list(csv.DictReader(f))

def write_race_table():
    header = [
        "Heatnumber", "Racenumber", "Lane", "StartTime",
        "FirstPass",
        *[f"Dog{i}" for i in range(1, 10)],
        "RaceTime", "ModifiedDate", "User"
    ]

    start_fmt = datetime.fromtimestamp(green_time).strftime("%Y-%m-%d %H:%M:%S") if green_time else ""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_header = not os.path.exists(RACE_TABLE)

    with open(RACE_TABLE, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        for lane in ["Left", "Right"]:
            splits = team_splits[lane]
            race_time = sum(s for s in splits if isinstance(s, (int, float)))
            row = [
                heat_number,
                race_number,
                lane,
                start_fmt
            ] + [
                f"{s:.3f}" if s else "" for s in splits
            ] + [
                f"{race_time:.3f}",
                now_str,
                USER_NAME
            ]
            w.writerow(row)

def lane_aligned(lane: str) -> bool:
    """
    Return True if both sensors for the given lane are clear (untriggered).
    Inputs are active-low: 0 = triggered, 1 = clear.
    """
    state = expander.read_state()
    if lane == "Left":
        enter_pin = PIN_LEFT_ENTER
        exit_pin  = PIN_LEFT_EXIT
    else:
        enter_pin = PIN_RIGHT_ENTER
        exit_pin  = PIN_RIGHT_EXIT

    enter_clear = bool(state & (1 << enter_pin))
    exit_clear  = bool(state & (1 << exit_pin))
    return enter_clear and exit_clear

def all_lanes_aligned():
    return lane_aligned("Left") and lane_aligned("Right")

def update_ready_lights():
    """
    FINAL CORRECT BEHAVIOR:
    - Pin 7: Judge Ready indicator ONLY.
    - Pin 8 & Pin 9: ALWAYS show sensor alignment when a race is NOT running.
    - ALL three OFF when race is running.
    """
    try:
        # During race: ALL ready lights off
        if race_running:
            expander.set_pin(PIN_READY_1, False)
            expander.set_pin(PIN_READY_2, False)
            expander.set_pin(PIN_READY_3, False)
            return

        # ---- RACE NOT RUNNING ----

        # Pin 7 = Judge Ready light
        expander.set_pin(PIN_READY_1, judge_ready_flag)

        # Pins 8 and 9 = sensor alignment ALWAYS (when not racing)
        left_ok = lane_aligned("Left")
        right_ok = lane_aligned("Right")

        expander.set_pin(PIN_READY_2, left_ok)
        expander.set_pin(PIN_READY_3, right_ok)

    except Exception as e:
        print("Ready light update error:", e)


def stop_and_save_race():
    global race_running, last_status, judge_ready_flag

    if race_running:
        race_running = False
        write_race_table()
        # ---- Determine winner and turn on winner light ----
        try:
            left_times = [t for t in team_splits["Left"] if isinstance(t, (int, float))]
            right_times = [t for t in team_splits["Right"] if isinstance(t, (int, float))]

            left_total = sum(left_times) if left_times else None
            right_total = sum(right_times) if right_times else None

            print("Totals:", left_total, right_total)


            # Only evaluate if at least one split exists 
            if left_total is not None or right_total is not None:
                if left_total is not None and (right_total is None or left_total < right_total):
                    # Left wins
                    winner_light_for_2_minutes("Left")
                    last_status = f"🏆 LEFT lane wins! ({left_total:.3f}s)"
                elif right_total is not None and (left_total is None or right_total < left_total):
                    # Right wins
                    winner_light_for_2_minutes("Right")
                    last_status = f"🏆 RIGHT lane wins! ({right_total:.3f}s)"
                else:
                    last_status = "Tie — no winner light"
        except Exception as e:
            print("Winner light error:", e)

        #BUG: this overwrites the winner announcement
        #FIXME:
        last_status = f"Race {race_number}, Heat {heat_number} stopped — results saved."

        # Turn off judge ready after the race
        judge_ready_flag = False
        update_ready_lights()

    else:
        last_status = "No race running."

    save_live_state()


def save_live_state():
    with open(LIVE_STATE, "w") as f:
        json.dump({
            "race_running": race_running,
            "status": last_status,
            "splits": team_splits,
            "teams": current_teams,
            "race_number": race_number,
            "heat_number": heat_number,
            "judge_ready": judge_ready_flag,
            "ts": time.time(),
            "green": green_time,

            # NEW warmup fields
            "warmup_active": warmup_active,
            "warmup_end": warmup_end_ts,
            "warmup_remaining": warmup_remaining_seconds()
        }, f)


# ---------------- Race Logic ----------------
def lane_for_pin(pin):
    if pin in (PIN_LEFT_ENTER, PIN_LEFT_EXIT):
        return "Left"
    elif pin in (PIN_RIGHT_ENTER, PIN_RIGHT_EXIT):
        return "Right"
    return None

def handle_button(pin):
    global green_time, race_running, team_splits, finish_times
    global dog_index, lane_red, dog_entered, dog_exited, last_status, expander

    if not race_running:
        return

    now = time.time()

    if pin in [PIN_LEFT_ENTER, PIN_LEFT_EXIT]:
        lane = "Left"
        enter_pin, exit_pin, red_pin = PIN_LEFT_ENTER, PIN_LEFT_EXIT, PIN_LEFT_RED
    else:
        lane = "Right"
        enter_pin, exit_pin, red_pin = PIN_RIGHT_ENTER, PIN_RIGHT_EXIT, PIN_RIGHT_RED

    i = dog_index[lane]
    if i >= 10:
        return

    false_start = False

    if i == 0 and pin == enter_pin and (green_time is None or now < green_time):
        false_start = True
    elif i > 0 and pin == enter_pin and not dog_exited[lane]:
        false_start = True

    if false_start:

        flash_false_start_light(red_pin)
        lane_red[lane] = True

        # ------------------------------------------
        # FIRST DOG EARLY START (use 4-second lights)
        # ------------------------------------------
        if i == 0 and pin == enter_pin:
            if start_sequence_begin is not None:
                elapsed = now - start_sequence_begin
                remaining = 4.0 - elapsed
                if remaining < 0:
                    remaining = 0.0

                early_value = -round(remaining, 3)

                # Log early-start time into REACTION TIME slot
                team_splits[lane][0] = early_value
                finish_times[lane][0] = now
                dog_start_times[lane][0] = now

                print(f"{lane} REACTION TIME EARLY START logged {early_value:.3f}s")

                # Now allow Dog 1 to run next normally
                dog_index[lane] = 1
                dog_entered[lane] = False
                dog_exited[lane] = True

                last_status = f"🚨 EARLY START — {lane} lane ({early_value:.3f}s)"
                save_live_state()
                return
            else:
                print("WARNING: start_sequence_begin missing for early-start calc")

        # ------------------------------------------
        # Non-first dog false start (your old logic)
        # ------------------------------------------
        dog_index[lane] += 1
        i = dog_index[lane]

        last_status = f"🚨 FALSE START — {lane} lane (Dog {i})"
        print(last_status)


    if pin == enter_pin:

        # Store the entrance time
        dog_start_times[lane][i] = now

        # Calculate correct split
        if i == 0:
            split = round(now - green_time, 3)
        else:
            prev_start = dog_start_times[lane][i - 1]

            # If previous dog hasn't started yet (rare but possible)
            if prev_start is None:
                prev_start = now

            split = round(now - prev_start, 3)

        team_splits[lane][i] = split
        finish_times[lane][i] = now   # optional (exit will overwrite)

        print(f"{lane} Dog {i+1} ENTER — split LOGGED: {split:.3f}s")

        dog_entered[lane] = True
        dog_exited[lane] = False

        last_status = f"{lane} Dog {i+1} split: {split:.3f}s"
        save_live_state()
        return

    elif pin == exit_pin:

        # Dog must have entered before exiting
        if not dog_entered[lane]:
            print(f"{lane} EXIT ignored — no active dog_entered flag")
            return

        dog_entered[lane] = False
        dog_exited[lane]  = True

        # Advance to next dog
        dog_index[lane] += 1

        print(f"{lane} Dog {i+1} EXITED — moving to Dog {dog_index[lane]+1}")
        return

def button_watcher():
    global lane_delay_until
    prev_state = 0xFFFF
    print(">>> Button watcher running <<<")
    while True:
        try:
            state = expander.read_state()
            changed = prev_state ^ state
            now = time.time()

            for pin in [PIN_LEFT_ENTER, PIN_LEFT_EXIT, PIN_RIGHT_ENTER, PIN_RIGHT_EXIT]:
                if changed & (1 << pin):
                    # Active-low: triggered on falling edge
                    if not (state & (1 << pin)):
                        lane = lane_for_pin(pin)
                        if lane is None:
                            continue

                        # ---- 2-second lane delay ----
                        # If we are still inside the delay window for this lane,
                        # ignore BOTH entrance and exit events.
                        if now < lane_delay_until[lane]:
                            # Still in quiet window for this lane → ignore event
                            # print(f"Ignoring pin {pin} on {lane} (within 2s delay)")
                            continue

                        # ---- Per-pin debounce (50 ms) ----
                        if (now - last_press_time.get(pin, 0)) * 1000.0 < DEBOUNCE_MS:
                            continue
                        last_press_time[pin] = now

                        # Process the event normally
                        handle_button(pin)

                        # If this was an ENTRANCE sensor, start the 2s delay window
                        if pin in (PIN_LEFT_ENTER, PIN_RIGHT_ENTER):
                            lane_delay_until[lane] = now + ENTER_DELAY

            prev_state = state
            time.sleep(0.02)

        except Exception as e:
            print(f"⚠️ Button watcher error: {e}")
            time.sleep(0.2)

# ---------------- Lighting ----------------
def start_sequence():
    global green_time, start_sequence_begin
    try:
        # Reset lights
        for p in [PIN_READY_1, PIN_READY_2, PIN_READY_3]:
            expander.set_pin(p, False)
        for p in [PIN_LEFT_RED, PIN_RIGHT_RED, PIN_BLUE_TOP, PIN_YELLOW_TOP,
                      PIN_YELLOW_BOT, PIN_LEFT_GREEN, PIN_RIGHT_GREEN]:
            expander.set_pin(p, False)

        # ----------------------------------
        # START 4-SECOND COUNTDOWN
        # ----------------------------------
        # TODO: Why is shutting down the server needed ?
        start_sequence_begin = time.time() # BUG: This will cause the server to be frozen for 4 seconds

        # BLUE (second 0 → 1)
        expander.set_pin(PIN_BLUE_TOP, True)
        time.sleep(1)

        # YELLOW TOP (second 1 → 2)
        expander.set_pin(PIN_YELLOW_TOP, True)
        time.sleep(1)

        # YELLOW BOTTOM (second 2 → 3)
        expander.set_pin(PIN_YELLOW_BOT, True)
        time.sleep(1)

        # GREEN (second 3 → 4)
        expander.set_pin(PIN_LEFT_GREEN, True)
        expander.set_pin(PIN_RIGHT_GREEN, True)
        green_time = time.time()
        time.sleep(1)

        # Turn off sequence lights
        for p in [PIN_BLUE_TOP, PIN_YELLOW_TOP, PIN_YELLOW_BOT,
                      PIN_LEFT_GREEN, PIN_RIGHT_GREEN]:
            expander.set_pin(p, False)

    except Exception as e:
        print("Lighting error:", e)

# ---------------- Routes ----------------
@app.route("/")
def root_redirect():
    return redirect("/menu")

@app.route("/control")
def index():
    j_teams = {k: type("T", (), v) for k, v in teams.items()}
    return render_template_string(
        page_html,
        status=last_status,
        current_teams=current_teams,
        teams=j_teams,
        team_splits=team_splits,
        results=read_race_table(),
        heat_number=heat_number,
        race_number=race_number
    )

@app.route("/warmup_state")
def warmup_state():
    with warmup_lock:
        return jsonify({
            "active": warmup_active,
            "end_time": warmup_end_ts,            # seconds since epoch (float or None)
            "remaining": warmup_remaining_seconds(),
            "status": last_status
        })

@app.route("/mobile")
def mobile():
    return render_template_string(mobile_html)

@app.route("/menu")
def menu():
    return render_template_string(menu_html)

# ---- Mobile-friendly actions ----
@app.route("/red_light", methods=["POST"])
def red_light():
    global last_status
    expander.set_pin(PIN_LEFT_RED, True)
    expander.set_pin(PIN_RIGHT_RED, True)
    last_status = "🔴 Red lights turned on!"
    save_live_state()
    return redirect(url_for("mobile"))

@app.route("/finish_race", methods=["POST"])
def finish_race():
    stop_and_save_race()
    return redirect(url_for("mobile"))

@app.route("/judge_ready", methods=["POST"])
def judge_ready():
    global judge_ready_flag, last_status, team_splits, finish_times, dog_index, lane_red, race_running, green_time
    try:
        # Turn off race lights
        for p in [PIN_LEFT_RED, PIN_RIGHT_RED, PIN_BLUE_TOP, PIN_YELLOW_TOP,
                  PIN_YELLOW_BOT, PIN_LEFT_GREEN, PIN_RIGHT_GREEN]:
            expander.set_pin(p, False)

        # Reset internal state
        team_splits = {"Left": [None]*10, "Right": [None]*10}
        finish_times = {"Left": [None]*10, "Right": [None]*10}
        dog_index = {"Left": 0, "Right": 0}
        lane_red = {"Left": False, "Right": False}
        race_running = False
        green_time = None
        for lane in ["Left", "Right"]:
            dog_entered[lane] = False
            dog_exited[lane] = False

        judge_ready_flag = True
        last_status = "Judge Ready — race can begin."

        # Update ready/align lights:
        update_ready_lights()

    except Exception as e:
        last_status = f"Judge Ready error: {e}"
    save_live_state()
    # redirect back to where you came from
    return redirect(request.referrer or url_for("index"))

@app.route("/start_race", methods=["POST"])
def start_race():
    global race_running, green_time, race_number, last_status
    if not judge_ready_flag:
        last_status = "Judge not ready — press Judge Ready first."
        save_live_state()
        return redirect(request.referrer or url_for("index"))
    if not all_lanes_aligned():
        last_status = "Lane alignment required — both lanes must be clear (pins 8 & 9 ON)."
        save_live_state()
        return redirect(request.referrer or url_for("index"))

    for lane in ["Left", "Right"]:
        dog_entered[lane] = False
        dog_exited[lane] = False
        lane_red[lane] = False
        dog_index[lane] = 0

    race_running = True
    update_ready_lights()  # turn off ready lights when race starts

    last_status = f"Race {race_number}, Heat {heat_number} starting sequence..."
    save_live_state()
    start_sequence()
    last_status = f"Race {race_number}, Heat {heat_number} — GREEN!"
    save_live_state()

    # stay on mobile if started there
    if "/mobile" in (request.referrer or ""):
        return redirect(url_for("mobile"))
    return redirect(url_for("index"))

@app.route("/stop_race", methods=["POST"])
def stop_race():
    stop_and_save_race()
    return redirect(url_for("index"))

@app.route("/reset_heat", methods=["POST"])
def reset_heat():
    global team_splits, finish_times, dog_index, lane_red
    global race_running, green_time, judge_ready_flag
    global heat_number, last_status

    # increment heat but KEEP SAME RACE NUMBER
    heat_number += 1

    #Display reset
    os.makedirs("data", exist_ok=True)
    with open("data/display_reset.txt", "w") as f:
        f.write(str(time.time()))

    team_splits = {"Left": [None]*10, "Right": [None]*10}
    finish_times = {"Left": [None]*10, "Right": [None]*10}
    dog_index = {"Left": 0, "Right": 0}
    lane_red = {"Left": False, "Right": False}
    race_running = False
    green_time = None
    judge_ready_flag = False
    last_status = f"🔁 Race {race_number} — Heat {heat_number} started."

    update_ready_lights()
    save_live_state()
    return redirect(url_for("index"))

@app.route("/reset_race", methods=["POST"])
def reset_race():
    global team_splits, finish_times, dog_index, lane_red
    global race_running, green_time, judge_ready_flag
    global race_number, heat_number, last_status, dog_entered, dog_exited

    # increment race, reset heat to 1
    race_number += 1
    heat_number = 1

    # Reset display
    os.makedirs("data", exist_ok=True)
    with open("data/display_reset.txt", "w") as f:
        f.write(str(time.time()))

    team_splits = {"Left": [None]*10, "Right": [None]*10}
    finish_times = {"Left": [None]*10, "Right": [None]*10}
    dog_index = {"Left": 0, "Right": 0}
    lane_red = {"Left": False, "Right": False}
    dog_entered = {"Left": False, "Right": False}
    dog_exited = {"Left": False, "Right": False}
    race_running = False
    judge_ready_flag = False
    green_time = None

    last_status = f"✅ Race {race_number} — Heat {heat_number} started (new race)."

    update_ready_lights()
    save_live_state()
    return redirect(url_for("index"))

@app.route("/power_off", methods=["POST"])
def power_off():
    global team_splits, finish_times, dog_index, lane_red
    global race_running, green_time, race_number, heat_number
    global judge_ready_flag, last_status, dog_entered, dog_exited

    # FULL SYSTEM RESET
    race_number = 1
    heat_number = 1

    team_splits = {"Left": [None]*10, "Right": [None]*10}
    finish_times = {"Left": [None]*10, "Right": [None]*10}
    dog_index = {"Left": 0, "Right": 0}
    lane_red = {"Left": False, "Right": False}
    dog_entered = {"Left": False, "Right": False}
    dog_exited = {"Left": False, "Right": False}
    race_running = False
    judge_ready_flag = False
    green_time = None
    last_status = "🛑 System Reset — all races and heats cleared."

    if os.path.exists(RACE_TABLE):
        os.remove(RACE_TABLE)
    if os.path.exists(LIVE_STATE):
        os.remove(LIVE_STATE)

    update_ready_lights()
    save_live_state()
    return redirect(url_for("index"))

@app.route("/download_race_table")
def download_race_table():
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = "UFLI_races.csv"
    file_path = os.path.join(DATA_DIR, filename)
    all_rows = read_race_table()

    heat_str = str(heat_number)
    heat_rows = []
    for r in all_rows:
        key = None
        if "Heatnumber" in r:
            key = "Heatnumber"
        elif "HeatNumber" in r:
            key = "HeatNumber"
        elif "heatnumber" in r:
            key = "heatnumber"

        if key is None:
            if heat_number == 1:
                heat_rows.append(r)
            continue

        if (r.get(key) or "") == heat_str:
            heat_rows.append(r)

    if not heat_rows:
        with open(file_path, "w", newline="") as f:
            f.write(f"No race data recorded yet for Heat {heat_number}.\n")
        return send_file(file_path, as_attachment=True)

    fieldnames = list(heat_rows[0].keys())
    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(heat_rows)

    return send_file(file_path, as_attachment=True)

@app.route("/live_state")
def live_state():
    if os.path.exists(LIVE_STATE):
        try:
            with open(LIVE_STATE) as f:
                return jsonify(json.load(f))
        except json.JSONDecodeError:
            pass
    return jsonify({
        "race_running": race_running,
        "status": last_status,
        "splits": team_splits,
        "teams": current_teams,
        "race_number": race_number,
        "heat_number": heat_number,
        "judge_ready": judge_ready_flag,
        "green": green_time
    })

# ---------------- Warm-Up Timer Routes ----------------
@app.route("/warmup", methods=["GET"])
def warmup_page():
    return render_template_string(warmup_html)

@app.route("/start_warmup", methods=["POST"])
def start_warmup():
    mins = int(request.form.get("minutes", 0) or 0)
    secs = int(request.form.get("seconds", 0) or 0)
    total = max(0, mins*60 + secs)
    if total > 0:
        warmup_set(total)
        msg = f"Warm-Up started for {total} seconds..."
    else:
        warmup_cancel()
        msg = "Warm-Up cancelled."
    return f"<meta http-equiv='refresh' content='0.6;url=/warmup'><h2>{msg}</h2>"

@app.route("/cancel_warmup", methods=["POST"])
def cancel_warmup():
    warmup_cancel()
    time.sleep(0.2)
    return redirect(url_for("warmup_page"))


# ---------------- HTML ----------------
page_html = """
<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<title>FastPaw Race Control</title>
<style>
body{font-family:Arial;background:#fafafa;margin:30px;}
button{margin:6px;padding:8px 16px;font-size:15px;border:none;border-radius:8px;cursor:pointer;}
.start{background:#4CAF50;color:#fff;}
.stop{background:#2196F3;color:#fff;}
.download{background:#9C27B0;color:#fff;}
.reset_heat{background:#FF9800;color:#fff;}
.reset_race{background:#f44336;color:#fff;}
.secondary{background:#607D8B;color:#fff;}
.judge{background:#FFC107;color:#000;}
.poweroff{background:#000;color:#ff4444;font-weight:bold;}
.card{background:#fff;border-radius:10px;box-shadow:0 0 6px rgba(0,0,0,0.15);padding:10px;margin:12px auto;max-width:1000px;}
table{border-collapse:collapse;width:100%;}
th,td{padding:6px;border-bottom:1px solid #ddd;text-align:center;}
</style>
<script>
async function updateLive(){
  try{
    const r = await fetch('/live_state');
    const d = await r.json();
    document.getElementById('status').innerText = d.status || "";
    document.getElementById('race_info').innerText =
      "Race " + d.race_number + " — Heat " + d.heat_number;
    if (d.splits){
      for (const lane of Object.keys(d.splits)){
        const splits = d.splits[lane];
        for (let i=0;i<splits.length;i++){
          const el = document.getElementById(`${lane}_dog_${i}`);
          if (el) el.innerText = splits[i] ? Number(splits[i]).toFixed(3) : "--";
        }
      }
    }
    const delay=d.race_running?100:800;
    setTimeout(updateLive,delay);
  }catch(e){setTimeout(updateLive,800);}
}
window.onload=updateLive;
</script>
</head>
<body>
<h1>🏁 FastPaw Race Control</h1>
<div style="margin-bottom:10px;"><a href="/menu" style="text-decoration:none;">⬅️ Back to Menu</a></div>
<h3 id="race_info">Race {{ race_number }} — Heat {{ heat_number }}</h3>

<!-- Top control buttons -->
<div style="text-align:center;">
  <form action="{{ url_for('judge_ready') }}" method="post" style="display:inline;">
    <button class="judge">Judge Ready</button>
  </form>
  <form action="{{ url_for('start_race') }}" method="post" style="display:inline;">
    <button class="start">Start</button>
  </form>
  <form action="{{ url_for('stop_race') }}" method="post" style="display:inline;">
    <button class="stop">Stop</button>
  </form>
  <form action="{{ url_for('download_race_table') }}" method="get" style="display:inline;">
    <button class="download">Download CSV</button>
  </form>
  <form action="{{ url_for('reset_heat') }}" method="post" style="display:inline;">
    <button class="reset_heat">Reset Heat</button>
  </form>
  <form action="{{ url_for('reset_race') }}" method="post" style="display:inline;">
    <button class="reset_race">Reset Race</button>
  </form>
</div>

<h3>Status:</h3>
<div id="status" class="card">{{ status }}</div>

<div class="card">
  <h3>Live Splits</h3>
  <table>
    <tr><th>Pass/Dog</th><th>Left</th><th>Right</th></tr>

    <!-- Reaction Time row -->
    <tr>
    <td>Reaction Time</td>
    <td id="Left_dog_0">{{ team_splits["Left"][0] or "--" }}</td>
    <td id="Right_dog_0">{{ team_splits["Right"][0] or "--" }}</td>
    </tr>

    <!-- Dogs 1–9 -->
    {% for i in range(1,10) %}
    <tr>
        <td>Dog {{ i }}</td>
        <td id="Left_dog_{{i}}">{{ team_splits["Left"][i] or "--" }}</td>
        <td id="Right_dog_{{i}}">{{ team_splits["Right"][i] or "--" }}</td>
    </tr>
    {% endfor %}

  </table>
</div>

<div class="card" style="max-height:450px; overflow-y:auto;">
  <h3>Results</h3>
  <div style="max-height:350px; overflow-y:auto;">
    <table>
    <tr>
      <th>Heat</th><th>Race</th><th>Lane</th><th>Start</th>
      {% for i in range(1,11) %}<th>D{{i}}</th>{% endfor %}
      <th>Total</th><th>Modified</th><th>User</th>
    </tr>
    {% for r in results %}
      <tr>
        <td>{{r["Heatnumber"]}}</td>
        <td>{{r["Racenumber"]}}</td>
        <td>{{r["Lane"]}}</td>
        <td>{{r["StartTime"]}}</td>
        {% for i in range(1,11) %}<td>{{r["Dog"~i]}}</td>{% endfor %}
        <td>{{r["RaceTime"]}}</td>
        <td>{{r["ModifiedDate"]}}</td>
        <td>{{r["User"]}}</td>
      </tr>
    {% endfor %}
    </table>
  </div>
</div>

<!-- Power Off section -->
<div style="text-align:center;margin-top:40px;">
  <form action="{{ url_for('power_off') }}" method="post" style="display:inline;">
    <button class="poweroff">Reset System</button>
  </form>
  <p style="color:#555;margin-top:6px;">⚠️ This will clear all data and reset heats and races.</p>
</div>

</body>
</html>
"""

mobile_html = """
<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FastPaw Mobile Control</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:#fafafa;margin:0;padding:20px;text-align:center;}
h1{color:#222;}
button{width:80%;padding:15px;margin:10px 0;font-size:18px;border:none;border-radius:10px;cursor:pointer;}
.start{background:#4CAF50;color:#fff;font-weight:bold;}
.finish{background:#2196F3;color:#fff;font-weight:bold;}
.warmup{background:#FF9800;color:#fff;font-weight:bold;}
.status{margin-top:25px;padding:12px;border-radius:8px;background:#fff;box-shadow:0 0 6px rgba(0,0,0,0.1);}
a{color:#555;font-weight:bold;text-decoration:none;}
</style>
<script>
async function updateLive(){
  try{
    const r = await fetch('/live_state');
    const d = await r.json();
    document.getElementById('status').innerText = d.status || "";
    document.getElementById('race_info').innerText =
      "Race " + d.race_number + " — Heat " + d.heat_number;
    setTimeout(updateLive, 1000);
  }catch(e){setTimeout(updateLive, 2000);}
}
window.onload=updateLive;
</script>
</head>
<body>
<h1>🐾 FastPaw Mobile</h1>
<div style="margin-bottom:10px;"><a href="/menu">⬅️ Back to Menu</a></div>
<h3 id="race_info">Loading...</h3>

<form action="/start_race" method="post">
  <button class="start">🏁 Start Race</button>
</form>

<form action="/finish_race" method="post">
  <button class="finish">✅ Finish Race</button>
</form>

<!-- New: Warm-Up Timer -->
<form action="/warmup" method="get">
  <button class="warmup">⏱️ Warm-Up Timer</button>
</form>

<div id="status" class="status">Status: Loading...</div>
</body>
</html>
"""

warmup_html = """
<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Warm-Up Timer</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;background:#fafafa;margin:0;padding:20px;text-align:center;}
h1{color:#222;}
label{display:inline-block;width:90px;text-align:right;margin-right:8px;}
input[type=number]{width:90px;padding:8px;margin:6px;border-radius:8px;border:1px solid #ccc;font-size:18px;}
button{width:80%;max-width:360px;padding:15px;margin:10px 0;font-size:18px;border:none;border-radius:10px;cursor:pointer;}
.start{background:#FF9800;color:#fff;font-weight:bold;}
.cancel{background:#f44336;color:#fff;font-weight:bold;}
.back{background:#607D8B;color:#fff;font-weight:bold;}
.timer{font-size:64px;font-weight:bold;margin:20px;color:#222;}
.status{margin-top:10px;padding:10px;border-radius:8px;background:#fff;box-shadow:0 0 6px rgba(0,0,0,0.1);}
a{color:#555;font-weight:bold;text-decoration:none;}
</style>
<script>
let endTime = null;   // ms epoch
let active = false;
let tickTimer = null;

function fmt(secs){
  secs = Math.max(0, Math.floor(secs));
  const m = Math.floor(secs/60);
  const s = secs % 60;
  return (m<10?"0":"")+m+":"+(s<10?"0":"")+s;
}

async function refreshState(){
  try{
    const r = await fetch('/warmup_state', {cache:'no-store'});
    const d = await r.json();
    active = !!d.active;
    endTime = d.end_time ? (d.end_time*1000) : null;
    const remaining = d.remaining || 0;
    document.getElementById('remaining').innerText = fmt(remaining);
    document.getElementById('status').innerText = d.status || "";
    document.getElementById('activeFlag').innerText = active ? "Active" : "Inactive";
    if (!active){ endTime = null; }
  }catch(e){
    console.error("refreshState error:", e);
  }
}

function startTicker(){
  if (tickTimer) clearInterval(tickTimer);
  tickTimer = setInterval(async ()=>{
    if (active && endTime){
      const now = Date.now();
      const remMs = endTime - now;
      const rem = Math.max(0, Math.ceil(remMs/1000));
      document.getElementById('remaining').innerText = fmt(rem);
      if (rem <= 0){
        active = false;
        endTime = null;
        await refreshState();
      }
    }else{
      await refreshState();
    }
  }, 500);
}

window.onload = async function(){
  await refreshState();
  startTicker();
};
</script>
</head>
<body>
<h1>⏱️ Warm-Up Timer</h1>
<div style="margin-bottom:10px;"><a href="/mobile">⬅️ Back to Mobile</a></div>

<div class="timer" id="remaining">00:00</div>
<div class="status">
  <div><b>Warm-Up:</b> <span id="activeFlag">Inactive</span></div>
  <div id="status"></div>
</div>

<form action="/start_warmup" method="post" style="margin-top:15px;">
  <div>
    <label for="minutes">Minutes:</label>
    <input type="number" id="minutes" name="minutes" min="0" max="999" value="2">
  </div>
  <div>
    <label for="seconds">Seconds:</label>
    <input type="number" id="seconds" name="seconds" min="0" max="59" value="0">
  </div>
  <button class="start" type="submit">Start Countdown</button>
</form>

<form action="/cancel_warmup" method="post">
  <button class="cancel" type="submit">Cancel Warm-Up</button>
</form>

<div style="margin-top:20px;">
  <a class="back" href="/menu" style="display:inline-block;padding:12px 16px;border-radius:10px;background:#607D8B;color:#fff;">Back to Menu</a>
</div>
</body>
</html>
"""

menu_html = """
<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FastPaw Menu</title>
<style>
body { font-family: Arial, Helvetica, sans-serif; background: #f4f4f4; margin: 0; padding: 30px; text-align: center; }
h1 { color: #333; margin-bottom: 30px; }
.button { display: block; width: 80%; max-width: 300px; margin: 15px auto; padding: 15px; border: none; border-radius: 10px; font-size: 20px; font-weight: bold; cursor: pointer; color: white; text-decoration: none; }
.desktop { background: #2196F3; }
.mobile { background: #4CAF50; }
.footer { margin-top: 40px; font-size: 14px; color: #777; }
</style>
</head>
<body>
<h1>🐾 FastPaw System</h1>
<a class="button desktop" href="/control">Race Control (Desktop)</a>
<a class="button mobile" href="/mobile">FastPaw Mobile</a>
<div class="footer">BeaglePlay Control Interface</div>
</body>
</html>
"""

# ---------------- Main ----------------
if __name__ == "__main__":
    threading.Thread(target=button_watcher, daemon=True).start()
    threading.Thread(target=warmup_housekeeper, daemon=True).start()
    threading.Thread(target=alignment_poller, daemon=True).start()
    # Initialize ready lights once at startup
    update_ready_lights()
    app.run(host="0.0.0.0", port=5000, debug=True)