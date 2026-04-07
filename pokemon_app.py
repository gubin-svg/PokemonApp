import os
import requests
import streamlit as st
import sqlite3
import pandas as pd
import random
import altair as alt

DB_PATH = "pokemon.db"
DB_URL = "PUT_YOUR_DIRECT_DOWNLOAD_URL_HERE"

def ensure_db():
    if not os.path.exists(DB_PATH):
        r = requests.get(DB_URL, timeout=60)
        r.raise_for_status()
        with open(DB_PATH, "wb") as f:
            f.write(r.content)

ensure_db()

# ---------------------------
# DB
# ---------------------------
@st.cache_resource
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

conn = get_conn()

@st.cache_data
def get_names():
    return pd.read_sql_query("SELECT name FROM pokemon ORDER BY name;", conn)["name"].tolist()


@st.cache_data
def get_type_multiplier_map():
    df = pd.read_sql_query(
        "SELECT attacker_type, defender_type, multiplier FROM type_effectiveness;",
        conn,
    )
    return {
        (row["attacker_type"], row["defender_type"]): float(row["multiplier"])
        for _, row in df.iterrows()
    }


@st.cache_data
def get_table_columns(table_name):
    pragma_df = pd.read_sql_query(f"PRAGMA table_info({table_name});", conn)
    if pragma_df.empty:
        return set()
    return set(pragma_df["name"].tolist())

def refresh_names():
    st.cache_data.clear()
    return get_names()

names = get_names()

# ---------------------------
# LOAD POKEMON
# ---------------------------
def get_pokemon(name):
    query = """
    SELECT p.name, s.hp, s.attack, s.defense, s.speed,
           GROUP_CONCAT(t.type_name) as types
    FROM pokemon p
    JOIN stats s ON p.pokemon_id = s.pokemon_id
    JOIN pokemon_types pt ON p.pokemon_id = pt.pokemon_id
    JOIN types t ON pt.type_id = t.type_id
    WHERE p.name = ?
    GROUP BY p.pokemon_id;
    """
    df = pd.read_sql_query(query, conn, params=(name,))
    if df.empty:
        return None
    row = df.iloc[0]

    return {
        "name": row["name"],
        "max_hp": int(row["hp"]),
        "current_hp": int(row["hp"]),
        "attack": int(row["attack"]),
        "defense": int(row["defense"]),
        "speed": int(row["speed"]),
        "types": row["types"].split(",")
    }

# ---------------------------
# TYPE MULTIPLIER
# ---------------------------
def get_multiplier(attacker_types, defender_types):
    multiplier_map = get_type_multiplier_map()
    multiplier = 1.0
    for atk in attacker_types:
        for dfn in defender_types:
            mult = multiplier_map.get((atk, dfn), 1.0)
            multiplier *= mult
    return multiplier

# ---------------------------
# DAMAGE
# ---------------------------
def calc_damage(a, d):
    mult = get_multiplier(a["types"], d["types"])
    dmg = max(1, int(10 * (a["attack"] / max(1, d["defense"])) * mult))
    return dmg, mult

def attack(a, d):
    dmg, mult = calc_damage(a, d)
    d["current_hp"] -= dmg
    use_emoji = st.session_state.get("show_emojis", True)

    log = f"{a['name']} → {d['name']} | {dmg} dmg"

    if mult > 1:
        log += f" {'🔥 ' if use_emoji else ''}Super effective!"
    elif mult < 1:
        log += f" {'❄️ ' if use_emoji else ''}Not very effective."

    if d["current_hp"] <= 0:
        d["current_hp"] = 0
        log += f" {'💀 ' if use_emoji else ''}{d['name']} fainted"

    return log, dmg, mult

# ---------------------------
# TEAM HELPERS
# ---------------------------
def next_alive(team):
    for p in team:
        if p["current_hp"] > 0:
            return p
    return None

def has_alive(team):
    return any(p["current_hp"] > 0 for p in team)

def resolve_battle_turn():
    p = next_alive(st.session_state.player_team)
    a = next_alive(st.session_state.ai_team)

    if p is None or a is None:
        return False

    if p["speed"] > a["speed"]:
        order = [(p, a), (a, p)]
    elif a["speed"] > p["speed"]:
        order = [(a, p), (p, a)]
    else:
        order = random.sample([(p, a), (a, p)], 2)

    with conn:
        log, dmg, mult = attack(order[0][0], order[0][1])
        st.session_state.logs.append(log)
        save_log(
            st.session_state.battle_id,
            st.session_state.turn,
            order[0][0]["name"],
            order[0][1]["name"],
            dmg,
            mult,
            log,
        )

        if order[1][0]["current_hp"] > 0 and order[1][1]["current_hp"] > 0:
            log, dmg, mult = attack(order[1][0], order[1][1])
            st.session_state.logs.append(log)
            save_log(
                st.session_state.battle_id,
                st.session_state.turn,
                order[1][0]["name"],
                order[1][1]["name"],
                dmg,
                mult,
                log,
            )

    st.session_state.turn += 1
    return True

def auto_play_battle_to_end():
    while has_alive(st.session_state.player_team) and has_alive(st.session_state.ai_team):
        if not resolve_battle_turn():
            break

def save_log(battle_id, turn, attacker, defender, damage, mult, message):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO battle_log (battle_id, turn_number, attacker, defender, damage, multiplier, message)
        VALUES (?, ?, ?, ?, ?, ?, ?);
    """, (battle_id, turn, attacker, defender, damage, mult, message))

def create_battle():
    cursor = conn.cursor()
    cursor.execute("INSERT INTO battles (winner) VALUES (NULL);")
    conn.commit()
    return cursor.lastrowid

def set_battle_winner(battle_id, winner):
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE battles
        SET winner = ?
        WHERE battle_id = ?;
    """, (winner, battle_id))
    conn.commit()


def log_cheat_use(battle_id, cheat_code, details):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO cheat_audit (battle_id, cheat_code, details)
        VALUES (?, ?, ?);
    """, (battle_id, cheat_code, details))
    conn.commit()


def reset_cheats():
    cursor = conn.cursor()

    # Restore original stats
    cursor.execute("""
        UPDATE stats
        SET hp = (
                SELECT os.hp
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            attack = (
                SELECT os.attack
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            defense = (
                SELECT os.defense
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            sp_atk = (
                SELECT os.sp_atk
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            sp_def = (
                SELECT os.sp_def
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            speed = (
                SELECT os.speed
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            ),
            total = (
                SELECT os.total
                FROM original_stats os
                WHERE os.pokemon_id = stats.pokemon_id
            )
        WHERE pokemon_id IN (SELECT pokemon_id FROM original_stats);
    """)

    # Remove custom inserted legendary Pokémon
    custom_ids = pd.read_sql_query("""
        SELECT pokemon_id
        FROM pokemon
        WHERE pokedex_number = 9999;
    """, conn)

    if not custom_ids.empty:
        ids = custom_ids["pokemon_id"].tolist()
        placeholders = ",".join(["?"] * len(ids))

        cursor.execute(f"DELETE FROM pokemon_types WHERE pokemon_id IN ({placeholders});", ids)
        cursor.execute(f"DELETE FROM stats WHERE pokemon_id IN ({placeholders});", ids)
        cursor.execute(f"DELETE FROM pokemon WHERE pokemon_id IN ({placeholders});", ids)

    # Clear cheat audit
    cursor.execute("DELETE FROM cheat_audit;")

    conn.commit()

def cheat_upupdowndown(battle_id, team_names):
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(team_names))

    cursor.execute(f"""
        UPDATE stats
        SET hp = hp * 2
        WHERE pokemon_id IN (
            SELECT pokemon_id
            FROM pokemon
            WHERE name IN ({placeholders})
        );
    """, team_names)

    conn.commit()
    log_cheat_use(battle_id, "UPUPDOWNDOWN", f"Doubled HP for: {', '.join(team_names)}")


def cheat_godmode(battle_id, team_names):
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(team_names))

    cursor.execute(f"""
        UPDATE stats
        SET defense = 999
        WHERE pokemon_id IN (
            SELECT pokemon_id
            FROM pokemon
            WHERE name IN ({placeholders})
        );
    """, team_names)

    conn.commit()
    log_cheat_use(battle_id, "GODMODE", f"Set defense=999 for: {', '.join(team_names)}")


def cheat_legendary(battle_id, custom_name, hp, attack, defense, speed, type1, type2=None):
    cursor = conn.cursor()

    existing = pd.read_sql_query(
        "SELECT * FROM pokemon WHERE name = ?;",
        conn,
        params=(custom_name,)
    )

    if existing.empty:
        cursor.execute("""
            INSERT INTO pokemon (pokedex_number, name, generation, legendary)
            VALUES (?, ?, ?, ?);
        """, (9999, custom_name, 9, 1))

        pokemon_id = cursor.lastrowid
        total = hp + attack + defense + speed

        cursor.execute("""
            INSERT INTO stats (pokemon_id, hp, attack, defense, sp_atk, sp_def, speed, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (pokemon_id, hp, attack, defense, attack, defense, speed, total))

        type1_id = pd.read_sql_query(
            "SELECT type_id FROM types WHERE type_name = ?;",
            conn,
            params=(type1,)
        ).iloc[0]["type_id"]

        cursor.execute("""
            INSERT INTO pokemon_types (pokemon_id, type_id, slot)
            VALUES (?, ?, 1);
        """, (pokemon_id, int(type1_id)))

        if type2 and type2 != "None":
            type2_id = pd.read_sql_query(
                "SELECT type_id FROM types WHERE type_name = ?;",
                conn,
                params=(type2,)
            ).iloc[0]["type_id"]

            cursor.execute("""
                INSERT INTO pokemon_types (pokemon_id, type_id, slot)
                VALUES (?, ?, 2);
            """, (pokemon_id, int(type2_id)))

        conn.commit()

    details = f"Inserted custom legendary: {custom_name} | HP={hp}, ATK={attack}, DEF={defense}, SPEED={speed}, Type1={type1}, Type2={type2}"
    log_cheat_use(battle_id, "LEGENDARY", details)

def apply_cheat(battle_id, cheat_code, team_names, legendary_config=None):
    code = cheat_code.strip().upper()

    if code == "":
        return None

    if code == "UPUPDOWNDOWN":
        cheat_upupdowndown(battle_id, team_names)
        return None

    elif code == "GODMODE":
        cheat_godmode(battle_id, team_names)
        return None

    elif code == "LEGENDARY":
        if legendary_config is None:
            raise ValueError("Legendary cheat requires custom stats.")
        cheat_legendary(
            battle_id=battle_id,
            custom_name=legendary_config["name"],
            hp=legendary_config["hp"],
            attack=legendary_config["attack"],
            defense=legendary_config["defense"],
            speed=legendary_config["speed"],
            type1=legendary_config["type1"],
            type2=legendary_config["type2"]
        )
        return legendary_config["name"]

    else:
        raise ValueError("Invalid cheat code")


def reset_match_state():
    st.session_state.started = False
    st.session_state.logs = []
    st.session_state.turn = 1
    st.session_state.battle_id = None
    st.session_state.winner_saved = False
    st.session_state.player_team = []
    st.session_state.ai_team = []


def effectiveness_text(mult):
    if mult > 1:
        return "Super effective"
    if mult < 1:
        return "Not very effective"
    return "Neutral"


def render_pokemon_card(title, pokemon):
    st.markdown(f"### {title}")
    if pokemon is None:
        st.info("Not available")
        return

    st.markdown(f"**{pokemon['name']}**")
    st.caption(f"Type: {' / '.join(pokemon['types'])}")
    hp_ratio = pokemon["current_hp"] / max(1, pokemon["max_hp"])
    st.progress(hp_ratio)

    c1, c2 = st.columns(2)
    c1.metric("HP", f"{pokemon['current_hp']}/{pokemon['max_hp']}")
    c2.metric("Speed", pokemon["speed"])

    c3, c4 = st.columns(2)
    c3.metric("Attack", pokemon["attack"])
    c4.metric("Defense", pokemon["defense"])


def render_battle_feed_line(line, index=None):
    line_lower = line.lower()
    if "super effective" in line_lower:
        tone_class = "feed-super"
        tag = "SUPER"
    elif "not very effective" in line_lower:
        tone_class = "feed-weak"
        tag = "WEAK"
    else:
        tone_class = "feed-neutral"
        tag = "NEUTRAL"

    prefix = f"{index}. " if index is not None else ""
    st.markdown(
        (
            f"<div class='app-section-card feed-line {tone_class}'>"
            f"<span class='feed-tag'>{tag}</span> {prefix}{line}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render_history_screen():
    st.subheader("Battle History")
    history_limit = int(st.session_state.get("history_limit", 20))
    cheat_cols = get_table_columns("cheat_audit")
    has_cheat_audit = len(cheat_cols) > 0

    history_df = pd.read_sql_query(
        """
        SELECT
            b.battle_id,
            b.started_at,
            COALESCE(b.winner, 'Not finished') AS winner,
            COUNT(DISTINCT bl.log_id) AS attacks
        FROM battles b
        LEFT JOIN battle_log bl ON bl.battle_id = b.battle_id
        GROUP BY b.battle_id, b.started_at, b.winner
        ORDER BY b.battle_id DESC
        LIMIT ?;
        """,
        conn,
        params=(history_limit,),
    )

    if history_df.empty:
        st.info("No battles found yet.")
        return

    st.caption(f"Showing latest {len(history_df)} battles")
    history_display_df = history_df.reset_index().rename(columns={"index": "row"})
    history_display_df["row"] = history_display_df["row"] + 1
    st.dataframe(history_display_df, width="stretch", hide_index=True)

    selected_battle = st.selectbox(
        "Inspect battle details",
        history_df["battle_id"].tolist(),
        key="history_battle_select",
    )

    details_df = pd.read_sql_query(
        """
        SELECT turn_number, attacker, defender, damage, multiplier, message
        FROM battle_log
        WHERE battle_id = ?
        ORDER BY log_id ASC;
        """,
        conn,
        params=(selected_battle,),
    )

    if details_df.empty:
        st.info("No log entries for this battle.")
        return

    st.markdown("#### Selected Battle Timeline")
    for idx, msg in enumerate(details_df["message"].tolist(), start=1):
        render_battle_feed_line(msg, idx)

    if has_cheat_audit:
        global_cheats_df = pd.read_sql_query(
            """
            SELECT audit_id, cheat_code, details
            FROM cheat_audit
            ORDER BY audit_id DESC
            LIMIT ?;
            """,
            conn,
            params=(history_limit,),
        )
    else:
        global_cheats_df = pd.DataFrame(columns=["audit_id", "cheat_code", "details"])

    st.markdown("#### Recent Cheat Audit (All)")
    if global_cheats_df.empty:
        st.caption("No cheat audit records found.")
    else:
        st.dataframe(global_cheats_df, width="stretch")


def render_settings_screen():
    st.subheader("Settings")
    st.caption("Simple UI settings for your battle experience.")

    st.session_state.show_emojis = st.toggle(
        "Show emojis in battle logs",
        value=st.session_state.get("show_emojis", True),
        key="settings_show_emojis",
    )

    st.session_state.feed_lines = st.slider(
        "Live battle feed lines",
        min_value=5,
        max_value=25,
        value=int(st.session_state.get("feed_lines", 10)),
        step=1,
        key="settings_feed_lines",
    )

    st.session_state.history_limit = st.slider(
        "History screen max battles",
        min_value=10,
        max_value=100,
        value=int(st.session_state.get("history_limit", 20)),
        step=5,
        key="settings_history_limit",
    )


def set_flash_message(text, level="success"):
    st.session_state.flash_message = text
    st.session_state.flash_level = level


def render_flash_message(location="main"):
    flash_text = st.session_state.get("flash_message")
    flash_level = st.session_state.get("flash_level", "success")
    if not flash_text:
        return

    if location == "sidebar":
        st.markdown("---")
        if flash_level == "error":
            st.error(flash_text)
        elif flash_level == "warning":
            st.warning(flash_text)
        else:
            st.success(flash_text)

        if st.button("Dismiss Message", key="dismiss_flash_sidebar"):
            st.session_state.flash_message = ""
            st.session_state.flash_level = "success"


# ---------------------------
# INIT STATE
# ---------------------------
if "started" not in st.session_state:
    st.session_state.started = False
    st.session_state.logs = []
    st.session_state.turn = 1
    st.session_state.battle_id = None
    st.session_state.winner_saved = False
    st.session_state.player_team = []
    st.session_state.ai_team = []

if "show_emojis" not in st.session_state:
    st.session_state.show_emojis = True

if "feed_lines" not in st.session_state:
    st.session_state.feed_lines = 10

if "history_limit" not in st.session_state:
    st.session_state.history_limit = 20

if "flash_message" not in st.session_state:
    st.session_state.flash_message = ""

if "flash_level" not in st.session_state:
    st.session_state.flash_level = "success"

# ---------------------------
# UI
# ---------------------------
st.title("⚔️ Pokémon Battle Arena")

st.markdown(
    """
    <style>
    .app-section-card {
        border-radius: 14px;
        padding: 12px 14px;
        margin-bottom: 10px;
        border: 1px solid rgba(255, 255, 255, 0.14);
    }
    .player-card {
        background: linear-gradient(135deg, rgba(39, 174, 96, 0.18), rgba(39, 174, 96, 0.07));
        border-left: 6px solid #27ae60;
    }
    .ai-card {
        background: linear-gradient(135deg, rgba(231, 76, 60, 0.18), rgba(231, 76, 60, 0.07));
        border-left: 6px solid #e74c3c;
    }
    .summary-banner {
        border-radius: 12px;
        padding: 12px 16px;
        background: linear-gradient(90deg, rgba(52, 152, 219, 0.2), rgba(26, 188, 156, 0.14));
        border: 1px solid rgba(255, 255, 255, 0.14);
        margin-bottom: 12px;
    }
    .feed-line {
        display: block;
        margin-bottom: 8px;
        border-left-width: 6px;
    }
    .feed-super {
        border-left: 6px solid #2ecc71;
        background: linear-gradient(135deg, rgba(46, 204, 113, 0.16), rgba(46, 204, 113, 0.06));
    }
    .feed-weak {
        border-left: 6px solid #e67e22;
        background: linear-gradient(135deg, rgba(230, 126, 34, 0.16), rgba(230, 126, 34, 0.06));
    }
    .feed-neutral {
        border-left: 6px solid #3498db;
        background: linear-gradient(135deg, rgba(52, 152, 219, 0.16), rgba(52, 152, 219, 0.06));
    }
    .feed-tag {
        display: inline-block;
        font-size: 11px;
        letter-spacing: 0.4px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 999px;
        margin-right: 8px;
        background: rgba(255, 255, 255, 0.12);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Game Controls")
    screen_mode = st.radio(
        "Screen",
        ["Battle", "History", "Settings"],
        key="screen_mode_selector",
    )

    if st.session_state.started and screen_mode != "Battle":
        st.caption("Battle is paused while viewing this screen.")

    if st.session_state.started:
        st.caption(f"Battle ID: {st.session_state.battle_id}")
        st.caption(f"Current Turn: {st.session_state.turn}")

        with st.expander("AI random picks", expanded=False):
            for idx, ai_pokemon in enumerate(st.session_state.ai_team, start=1):
                st.write(f"{idx}. {ai_pokemon['name']} ({' / '.join(ai_pokemon['types'])})")

        if st.button("Restart Match", key="restart_sidebar"):
            reset_match_state()
            st.rerun()
    else:
        names = get_names()

        st.subheader("Cheat Console")
        cheat_code = st.selectbox(
            "Choose cheat",
            ["", "UPUPDOWNDOWN", "GODMODE", "LEGENDARY"],
            help=(
                "Apply optional cheats before battle start.\n\n"
                "UPUPDOWNDOWN: doubles your Pokemon's HP via an UPDATE query.\n\n"
                "GODMODE: sets all your Pokemon's Defense and Sp.Def to 999.\n\n"
                "LEGENDARY: inserts a custom overpowered Pokemon into the database."
            )
        )

        if cheat_code in ["UPUPDOWNDOWN", "GODMODE"]:
            cheat_targets = st.multiselect(
                "Targets",
                names,
                max_selections=3,
                key="cheat_targets_sidebar"
            )
            if st.button("Execute Cheat", key="exec_cheat_sidebar"):
                if len(cheat_targets) == 0:
                    st.error("Pick at least one target.")
                    st.stop()

                try:
                    apply_cheat(
                        battle_id=None,
                        cheat_code=cheat_code,
                        team_names=cheat_targets,
                        legendary_config=None
                    )
                    set_flash_message(f"{cheat_code} applied successfully.", "success")
                except ValueError as e:
                    set_flash_message(str(e), "error")

                refresh_names()
                st.rerun()

        elif cheat_code == "LEGENDARY":
            type_options = pd.read_sql_query(
                "SELECT type_name FROM types ORDER BY type_name;",
                conn
            )["type_name"].tolist()

            custom_name = st.text_input("Legendary name", value="ChatGPTmon", key="legend_name_sidebar")
            custom_hp = st.number_input("HP", min_value=100, max_value=999, value=400, step=10, key="legend_hp_sidebar")
            custom_attack = st.number_input("Attack", min_value=50, max_value=999, value=250, step=10, key="legend_atk_sidebar")
            custom_defense = st.number_input("Defense", min_value=50, max_value=999, value=250, step=10, key="legend_def_sidebar")
            custom_speed = st.number_input("Speed", min_value=50, max_value=999, value=220, step=10, key="legend_spd_sidebar")
            custom_type1 = st.selectbox("Primary type", type_options, key="legend_type1_sidebar")
            custom_type2 = st.selectbox("Secondary type", ["None"] + type_options, key="legend_type2_sidebar")

            if st.button("Insert Legendary", key="insert_legendary_sidebar"):
                if custom_name.strip() == "":
                    st.error("Legendary name cannot be empty.")
                    st.stop()

                legendary_config = {
                    "name": custom_name.strip(),
                    "hp": int(custom_hp),
                    "attack": int(custom_attack),
                    "defense": int(custom_defense),
                    "speed": int(custom_speed),
                    "type1": custom_type1,
                    "type2": custom_type2,
                }

                try:
                    apply_cheat(
                        battle_id=None,
                        cheat_code="LEGENDARY",
                        team_names=[],
                        legendary_config=legendary_config,
                    )
                    set_flash_message(f"{custom_name.strip()} inserted successfully.", "success")
                except ValueError as e:
                    set_flash_message(str(e), "error")

                refresh_names()
                st.rerun()

        if st.button("Reset Cheats", key="reset_cheats_sidebar"):
            reset_cheats()
            refresh_names()
            set_flash_message("Cheats reset. Database restored to baseline.", "success")
            st.rerun()

        render_flash_message(location="sidebar")


if screen_mode == "History":
    render_history_screen()
    st.stop()

if screen_mode == "Settings":
    render_settings_screen()
    st.stop()


if not st.session_state.started:
    st.subheader("Choose your battle team")

    picks = st.multiselect(
        "Pick 3 Pokemon for battle",
        get_names(),
        max_selections=3,
        key="battle_picks_main"
    )

    if picks:
        preview_cols = st.columns(min(3, len(picks)))
        for idx, name in enumerate(picks):
            with preview_cols[idx]:
                render_pokemon_card("Selection Preview", get_pokemon(name))

    st.divider()
    st.subheader("AI Team")
    ai_selection_mode = st.radio(
        "How would you like the AI team to be selected?",
        ["Random", "Choose Specific Pokemon"],
        key="ai_selection_mode"
    )

    ai_picks = []
    if ai_selection_mode == "Choose Specific Pokemon":
        available_for_ai = [name for name in get_names() if name not in picks]
        ai_picks = st.multiselect(
            "Pick 3 Pokemon for AI opponent",
            available_for_ai,
            max_selections=3,
            key="ai_picks_main"
        )
        if ai_picks:
            ai_preview_cols = st.columns(min(3, len(ai_picks)))
            for idx, name in enumerate(ai_picks):
                with ai_preview_cols[idx]:
                    render_pokemon_card("AI Preview", get_pokemon(name))

    if st.button("Start Battle", type="primary", key="start_battle_main"):
        if len(picks) != 3:
            st.error("Please pick exactly 3 Pokemon.")
            st.stop()

        st.session_state.player_team = [get_pokemon(name) for name in picks]
        if any(p is None for p in st.session_state.player_team):
            st.error("Could not load one or more selected Pokemon from the database.")
            st.stop()

        if ai_selection_mode == "Choose Specific Pokemon":
            if len(ai_picks) != 3:
                st.error("Please pick exactly 3 Pokemon for the AI.")
                st.stop()
            st.session_state.ai_team = [get_pokemon(name) for name in ai_picks]
            if any(p is None for p in st.session_state.ai_team):
                st.error("Could not load one or more AI Pokemon from the database.")
                st.stop()
        else:
            placeholders = ",".join(["?"] * len(picks))
            ai_query = (
                f"SELECT name FROM pokemon "
                f"WHERE legendary = 0 AND name NOT IN ({placeholders}) "
                f"ORDER BY RANDOM() LIMIT 3;"
            )
            ai_df = pd.read_sql_query(ai_query, conn, params=tuple(picks))
            if len(ai_df) < 3:
                st.error("Not enough eligible AI Pokemon after excluding your picks.")
                st.stop()

            st.session_state.ai_team = [get_pokemon(n) for n in ai_df["name"]]
            if any(p is None for p in st.session_state.ai_team):
                st.error("Could not load one or more AI Pokemon from the database.")
                st.stop()

        st.session_state.battle_id = create_battle()
        st.session_state.started = True
        st.session_state.logs = []
        st.session_state.turn = 1
        st.session_state.winner_saved = False
        st.rerun()


# ---------------------------
# GAME LOOP UI
# ---------------------------
if st.session_state.started:
    player_team = st.session_state.player_team
    ai_team = st.session_state.ai_team

    st.subheader(f"Turn {st.session_state.turn}")

    p = next_alive(player_team)
    a = next_alive(ai_team)

    if p is None or a is None:
        if has_alive(player_team):
            winner_text = "Player"
            st.success("Winner: Player")
            if not st.session_state.winner_saved:
                set_battle_winner(st.session_state.battle_id, "Player")
                st.session_state.winner_saved = True
        else:
            winner_text = "AI"
            st.error("Winner: AI")
            if not st.session_state.winner_saved:
                set_battle_winner(st.session_state.battle_id, "AI")
                st.session_state.winner_saved = True

        st.subheader("Battle Summary")
        st.markdown(
            f"<div class='summary-banner'><b>Battle Finished</b> | Winner: <b>{winner_text}</b></div>",
            unsafe_allow_html=True,
        )

        result_df = pd.read_sql_query(
            """
            SELECT battle_id, started_at, winner
            FROM battles
            WHERE battle_id = ?;
            """,
            conn,
            params=(st.session_state.battle_id,),
        )

        total_attacks = len(st.session_state.logs)
        total_turns = st.session_state.turn - 1 if total_attacks > 0 else 0
        best_hit = pd.read_sql_query(
            """
            SELECT attacker, defender, damage, multiplier
            FROM battle_log
            WHERE battle_id = ?
            ORDER BY damage DESC, log_id ASC
            LIMIT 1;
            """,
            conn,
            params=(st.session_state.battle_id,),
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Battle ID", int(result_df.iloc[0]["battle_id"]))
        m2.metric("Winner", winner_text)
        m3.metric("Turns", total_turns)
        m4.metric("Total attacks", total_attacks)

        st.caption(f"Started at: {result_df.iloc[0]['started_at']}")

        if not best_hit.empty:
            hit = best_hit.iloc[0]
            st.info(
                f"Highlight play: {hit['attacker']} hit {hit['defender']} for {int(hit['damage'])} damage "
                f"({effectiveness_text(float(hit['multiplier']))})."
            )

        damage_rows = pd.read_sql_query(
            """
            SELECT attacker, COUNT(*) AS attacks, SUM(damage) AS total_damage
            FROM battle_log
            WHERE battle_id = ?
            GROUP BY attacker
            ORDER BY total_damage DESC;
            """,
            conn,
            params=(st.session_state.battle_id,),
        )

        if not damage_rows.empty:
            st.markdown("#### Damage Leaders")

            player_names = {member["name"] for member in st.session_state.player_team}
            ai_names = {member["name"] for member in st.session_state.ai_team}

            damage_rows["team"] = damage_rows["attacker"].apply(
                lambda n: "Player" if n in player_names else ("AI" if n in ai_names else "Other")
            )

            chart = (
                alt.Chart(damage_rows)
                .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
                .encode(
                    x=alt.X("attacker:N", sort="-y", title="Pokemon", axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("total_damage:Q", title="Total Damage"),
                    color=alt.Color(
                        "team:N",
                        scale=alt.Scale(
                            domain=["Player", "AI", "Other"],
                            range=["#27ae60", "#e74c3c", "#3498db"],
                        ),
                        legend=alt.Legend(title="Team"),
                    ),
                    tooltip=["attacker", "team", "attacks", "total_damage"],
                )
            )
            st.altair_chart(chart, width='stretch')

        st.markdown("#### Final Team Status")
        fcol1, fcol2 = st.columns(2)
        with fcol1:
            st.markdown("**Player Team**")
            for member in st.session_state.player_team:
                hp_pct = int((member["current_hp"] / max(1, member["max_hp"])) * 100)
                st.markdown(
                    (
                        "<div class='app-section-card player-card'>"
                        f"<b>{member['name']}</b><br>"
                        f"HP: {member['current_hp']}/{member['max_hp']} ({hp_pct}%)<br>"
                        f"Type: {' / '.join(member['types'])}"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )
        with fcol2:
            st.markdown("**AI Team**")
            for member in st.session_state.ai_team:
                hp_pct = int((member["current_hp"] / max(1, member["max_hp"])) * 100)
                st.markdown(
                    (
                        "<div class='app-section-card ai-card'>"
                        f"<b>{member['name']}</b><br>"
                        f"HP: {member['current_hp']}/{member['max_hp']} ({hp_pct}%)<br>"
                        f"Type: {' / '.join(member['types'])}"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

        st.markdown("#### Battle Timeline")
        for idx, line in enumerate(reversed(st.session_state.logs[-20:]), start=1):
            render_battle_feed_line(line, idx)

        end_c1, end_c2 = st.columns(2)
        with end_c1:
            if st.button("Play Again", type="primary", key="play_again_end"):
                reset_match_state()
                st.rerun()
        with end_c2:
            if st.button("Reset Cheats and Play", key="reset_and_play_end"):
                reset_cheats()
                refresh_names()
                reset_match_state()
                st.rerun()

        st.stop()

    col1, col2 = st.columns(2)
    with col1:
        render_pokemon_card("Player Active", p)
    with col2:
        render_pokemon_card("AI Active", a)

    action_col1, action_col2 = st.columns(2)
    with action_col1:
        if st.button("Next Turn", type="primary", key="next_turn_main"):
            if resolve_battle_turn():
                st.rerun()
            st.rerun()
    with action_col2:
        if st.button("Auto Play to End", key="auto_play_main"):
            auto_play_battle_to_end()
            st.rerun()

    st.markdown("#### Live Battle Feed")
    max_feed = int(st.session_state.get("feed_lines", 10))
    for line in reversed(st.session_state.logs[-max_feed:]):
        render_battle_feed_line(line)
        