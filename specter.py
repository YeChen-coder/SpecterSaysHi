"""Frigate MQTT -> owner conversations, one-shot cat alerts, and situation nudges."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from availability import CoverDetector, dictating_app, frame_brightness
from cat_alert import CatArrivalGate, CatIdentifier, OneShotCatAnnouncer, resolve_cat_identity
from foci_state import FociAssessment, watch_foci
from log_retention import RetainedRotatingFileHandler
from mini_audio_publisher import AVATAR_ENV_KEYS
from nudge_prompt import generate_phone_text, phone_fallback_text, phone_nudge_payload
from nudge_voice import stay_audio, synthesize
from phone_guard import bounce_audio, guard_payload
from presence import OwnerPresence, PresenceGate
from phone_link import start_background as start_phone_link
from realtime_agent import RealtimeConversation, audio_device, sdk_tracing_enabled
from session_control import consume_start_request
from session_gate import SessionGate
from session_memory import consolidation_due, consolidate_memory
from situation import (
    INPUT_TOPICS as SITUATION_TOPICS, TOPIC_FOCI, TOPIC_GUARD, TOPIC_NUDGE_REQUEST, TOPIC_NUDGE_RESULT,
    TOPIC_PRESENCE, TOPIC_SESSION, Decision, SituationEngine, SituationSettings,
)
from tiled_cat import TiledCatArrivalGate, TiledCatDetector
from visual import FrigateFrameSource, VisualUnavailable

LOG = logging.getLogger("specter")


def load_env_file(path: Path, allowed: set[str] | None = None) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if allowed is not None and name not in allowed:
            continue
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))


def acquire_instance_lock(log_dir: Path):
    """Keep one MQTT listener even if a Windows task leaves a child process alive."""
    lock_file = (log_dir / "specter.lock").open("a+b")
    if lock_file.seek(0, os.SEEK_END) == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def main() -> None:
    import paho.mqtt.client as mqtt

    log_dir = Path(__file__).with_name("logs")
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RetainedRotatingFileHandler(
                log_dir / "specter.log", max_bytes=256 * 1024,
                backup_count=24, max_age_days=7,
            ),
        ],
    )
    instance_lock = acquire_instance_lock(log_dir)
    if instance_lock is None:
        LOG.warning("Another specter.py listener is already running")
        raise SystemExit(42)
    # EN: A previous process may have exited before clearing its session marker.
    # 中文：上一个进程若异常退出，启动时清除失效的会话标记。
    for marker in ("active_session.json", "close_session.json"):
        (log_dir / marker).unlink(missing_ok=True)
    load_env_file(Path(__file__).with_name(".env"), AVATAR_ENV_KEYS)
    load_env_file(Path(__file__).with_name("config.local.env"))
    existing = os.getenv("SPECTER_EXISTING_ENV", "")
    if existing:
        load_env_file(Path(existing), {
            "OPENAI_API_KEY", "OPENAI_REALTIME_MODEL", "OPENAI_REALTIME_VOICE",
            "OPENAI_REALTIME_OUTPUT_SPEED", "OPENAI_INPUT_TRANSCRIPTION_MODEL",
            "REALTIME_VAD_THRESHOLD",
        })
    target = os.getenv("SPECTER_TARGET_NAME", "").strip()
    if not target:
        raise SystemExit("Set SPECTER_TARGET_NAME to your exact Frigate Face Library name")
    visual_enabled = os.getenv("SPECTER_VISUAL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    frame_source = FrigateFrameSource(
        base_url=os.getenv("SPECTER_FRIGATE_URL", "http://127.0.0.1:15000"),
        camera_name=os.getenv("SPECTER_CAMERA_NAME", "desk_camera"),
        width=int(os.getenv("SPECTER_IMAGE_WIDTH", "768")),
        quality=int(os.getenv("SPECTER_IMAGE_QUALITY", "75")),
    ) if visual_enabled else None
    gate = PresenceGate(
        target_name=target,
        camera_name=os.getenv("SPECTER_CAMERA_NAME", "desk_camera"),
        min_face_score=float(os.getenv("SPECTER_MIN_FACE_SCORE", "0.90")),
        min_person_area=int(os.getenv("SPECTER_MIN_PERSON_AREA", "40000")),
        absence_seconds=float(os.getenv("SPECTER_ABSENCE_SECONDS", "25")),
        cooldown_seconds=float(os.getenv("SPECTER_COOLDOWN_SECONDS", "90")),
    )
    # EN: Disable only camera-initiated greetings; manual sessions still use
    # the same Realtime agent, and the cat alert has its own independent path.
    # 中文：只关闭摄像头主动问候；手动会话仍可启动，猫提醒也走独立流程。
    proactive_greeting_enabled = os.getenv(
        "SPECTER_PROACTIVE_GREETING_ENABLED", "true",
    ).strip().lower() in {"1", "true", "yes", "on"}
    dry_run = "--dry-run" in sys.argv
    listener_started = time.monotonic()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def publish_json(topic: str, payload: dict, retain: bool = False) -> None:
        client.publish(topic, json.dumps({**payload, "emitted_at_ms": int(time.time() * 1000)}), retain=retain)

    # EN: The situation engine fuses desk presence, FOCI and phone state that
    # travel over MQTT. Phase 1 only logs the nudges it would deliver.
    # 中文：情境引擎融合 MQTT 上的桌前在场、FOCI 与手机状态；第一阶段只记录本会发出的提醒。
    situation_enabled = os.getenv("SPECTER_SITUATION_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    owner_presence = OwnerPresence(
        target_name=target,
        camera_name=gate.camera_name,
        min_face_score=gate.min_face_score,
        min_person_area=gate.min_person_area,
        stale_seconds=float(os.getenv("SPECTER_PRESENCE_STALE_SECONDS", "180")),
        inherit_seconds=float(os.getenv("SPECTER_PRESENCE_INHERIT_SECONDS", "60")),
        zones=frozenset(
            zone.strip() for zone in os.getenv("SPECTER_PRESENCE_ZONES", "").split(",") if zone.strip()
        ),
    ) if situation_enabled else None
    situation = SituationEngine(SituationSettings.from_env()) if situation_enabled else None
    situation_lock = threading.Lock()
    # EN: Phase 2 voice delivery stays off until explicitly enabled; only the
    # listed channels (default: desk) are spoken through the room speaker.
    # 中文：第二阶段语音投递需显式开启；只有列出的渠道（默认 desk）通过房间扬声器说出。
    situation_voice = os.getenv("SPECTER_SITUATION_VOICE", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    voice_channels = frozenset(
        channel.strip() for channel in os.getenv("SPECTER_NUDGE_VOICE_CHANNELS", "desk").split(",")
        if channel.strip()
    )

    # EN: Phone notifications (bedtime by default) are used whenever voice does
    # not handle the nudge and the phone has reported within the last 45 s.
    # 中文：语音不负责该提醒、且手机 45 秒内上报过时，用手机通知送达（默认只用于催睡觉）。
    phone_nudges = os.getenv("SPECTER_NUDGE_PHONE", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    phone_rules = frozenset(
        rule.strip() for rule in os.getenv("SPECTER_NUDGE_PHONE_RULES", "bedtime").split(",")
        if rule.strip()
    )
    # EN: The phone also speaks each reminder in his voice and keeps a wind-down ready.
    # 中文：手机同时用他的声音读出提醒，并备好"Stay with me"放松引导。
    phone_voice = os.getenv("SPECTER_NUDGE_PHONE_VOICE", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    phone_link = None

    def publish_session_state(kind: str | None) -> None:
        publish_json(TOPIC_SESSION, {"active": kind}, retain=True)

    session_gate = SessionGate(on_change=publish_session_state)
    # EN: Availability: the camera cover is a do-not-disturb switch, and another
    # app dictating means the owner is talking to something else.
    # 中文：可打扰状态：摄像头盖子是勿扰开关；别的程序在听写说明用户在跟别的东西说话。
    cover_dnd = os.getenv("SPECTER_COVER_DND", "true").strip().lower() in {"1", "true", "yes", "on"}
    dictation_quiet = os.getenv("SPECTER_DICTATION_QUIET", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    cover = CoverDetector(dark_below=float(os.getenv("SPECTER_COVER_DARK_BELOW", "25")))

    def busy_reason() -> str | None:
        if cover_dnd and cover.covered:
            return "camera cover is closed"
        if dictation_quiet:
            app = dictating_app()
            if app:
                return f"{app} is using the microphone"
        return None

    def make_interrupt():
        """Stop a running session when dictation starts or the cover is closed after it began."""
        started_covered = cover.covered

        def interrupt() -> str | None:
            if dictation_quiet:
                app = dictating_app()
                if app:
                    return f"{app} started using the microphone"
            if cover_dnd and cover.covered and not started_covered:
                return "camera cover was closed"
            return None

        return interrupt
    audio_active = threading.Event()
    memory_saved = threading.Event()

    # EN: Cat identification runs in a separate worker so MQTT remains responsive.
    # 中文：猫的身份核对在独立线程里执行，避免网络等待阻塞 Frigate 事件接收。
    cat_enabled = os.getenv("SPECTER_CAT_ALERT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    cat_play_audio = os.getenv("SPECTER_CAT_PLAY_AUDIO", "true").strip().lower() in {"1", "true", "yes", "on"}
    cat_lock = threading.Lock()
    cat_worker_lock = threading.Lock()
    last_cat_announcement = [float("-inf")]
    cat_gate = None
    cat_identifier = None
    cat_announcer = None
    tiled_gate = None
    tiled_detector = None
    cat_frame_source = frame_source
    if cat_enabled:
        reference_dir = Path(os.getenv("SPECTER_CAT_REFERENCE_DIR", "private/cats/Tabo"))
        if not reference_dir.is_absolute():
            reference_dir = Path(__file__).parent / reference_dir
        chichi_reference_dir = Path(os.getenv(
            "SPECTER_CAT_CHICHI_REFERENCE_DIR", "private/cats/chichi",
        ))
        if not chichi_reference_dir.is_absolute():
            chichi_reference_dir = Path(__file__).parent / chichi_reference_dir
        cat_gate = CatArrivalGate(
            camera_name=gate.camera_name,
            min_area=int(os.getenv("SPECTER_CAT_MIN_AREA", "8000")),
            absence_seconds=float(os.getenv("SPECTER_CAT_ABSENCE_SECONDS", "30")),
            cooldown_seconds=float(os.getenv("SPECTER_CAT_COOLDOWN_SECONDS", "300")),
            classifier_model=os.getenv("SPECTER_CAT_CLASSIFIER_MODEL", "household_cats"),
        )
        cat_frame_source = frame_source or FrigateFrameSource(
                os.getenv("SPECTER_FRIGATE_URL", "http://127.0.0.1:15000"), gate.camera_name,
            )
        cat_identifier = CatIdentifier(
            cat_frame_source,
            reference_dir,
            os.getenv("SPECTER_CAT_VISION_MODEL", "gpt-5.4-mini"),
            chichi_reference_dir,
        )
        cat_announcer = OneShotCatAnnouncer(audio_active)
        if os.getenv("SPECTER_CAT_TILE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
            model_path = Path(__file__).parent / "models" / "ssdlite_mobilenet_v2.xml"
            try:
                tiled_detector = TiledCatDetector(
                    model_path,
                    min_score=float(os.getenv("SPECTER_CAT_TILE_MIN_SCORE", "0.55")),
                    min_area=int(os.getenv("SPECTER_CAT_MIN_AREA", "8000")),
                )
                tiled_gate = TiledCatArrivalGate(
                    absence_seconds=cat_gate.absence_seconds,
                    cooldown_seconds=cat_gate.cooldown_seconds,
                )
            except Exception:
                LOG.exception("Local tiled cat scan unavailable; Frigate cat events remain active")
        if cat_play_audio and not dry_run:
            # EN: Prepare the fixed clip in the background, without playing it.
            # 中文：后台预生成固定播报，猫出现时只需播放，缩短提示延迟。
            def prepare_cat_audio():
                try:
                    cat_announcer.prepare()
                    cat_announcer.prepare("chichi")
                    cat_announcer.prepare("cat")
                    LOG.info("One-shot cat speech prepared in memory")
                except Exception:
                    LOG.exception("Could not pre-generate cat speech; will retry on arrival")

            threading.Thread(target=prepare_cat_audio, name="cat-tts-prep", daemon=True).start()

    def start_conversation(
        kind: str = "greeting", assessment: FociAssessment | None = None,
        nudge: dict | None = None, wake_text: str = "",
    ) -> bool:
        if dry_run:
            LOG.info("DRY RUN: would start %s session", kind)
            return True

        def run() -> None:
            conversation = RealtimeConversation(
                target,
                # EN: With the cover closed there is nothing to see; say so from the start.
                # 中文：盖子关着时画面什么也没有，从一开始就告诉他看不见。
                frame_source=frame_source if kind in {"greeting", "wake"} and not (cover_dnd and cover.covered) else None,
                external_audio_active=audio_active,
                memory_saved_event=memory_saved,
                session_kind=kind,
                foci_context=assessment.reason if assessment else "",
                nudge=nudge,
                interrupt=make_interrupt(),
                wake_text=wake_text,
            )
            failed = True
            try:
                conversation.run()
                failed = False
            finally:
                if nudge is not None:
                    # EN: A crashed session is a delivery failure, not the user ignoring it.
                    # 中文：会话异常属于投递失败，不算用户忽略，不触发退避。
                    outcome, minutes = ("failed", None) if failed else conversation.nudge_outcome()
                    LOG.info("Nudge %s outcome: %s", nudge["rule"], outcome)
                    publish_json(TOPIC_NUDGE_RESULT, {
                        "id": nudge["id"], "outcome": outcome, "snooze_minutes": minutes,
                    })

        started = session_gate.start(kind, run)
        if not started and kind == "greeting":
            LOG.info("%s session skipped; %s session is active", kind, session_gate.active_kind)
        return started

    def on_foci_trigger(assessment: FociAssessment) -> bool:
        reason = busy_reason()
        if reason:
            # EN: Not accepted, so the evaluator keeps trying while the state persists.
            # 中文：不接受这次触发；状态持续不佳时，等可打扰后会再次触发。
            LOG.info("FOCI check-in held: %s", reason)
            return False
        started = start_conversation("foci_checkin", assessment)
        if started:
            LOG.info(
                "FOCI check-in triggered (%s, adverse %.0f%% over %.0fs)",
                assessment.reason, assessment.adverse_fraction * 100,
                assessment.duration_seconds,
            )
        return started

    foci_enabled = os.getenv("SPECTER_FOCI_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    LOG.info("Availability: cover do-not-disturb %s, quiet while dictating %s",
             "on" if cover_dnd else "off", "on" if dictation_quiet else "off")
    LOG.info("Proactive triggers: greeting %s, FOCI %s, situation %s",
             "on" if proactive_greeting_enabled else "off",
             "on" if foci_enabled else "off",
             ("+".join(name for name, on in (("voice", situation_voice), ("phone", phone_nudges)) if on)
              or "silent test") if situation_enabled else "off")
    foci_relay: dict = {"connected": None, "label": None, "at": float("-inf")}

    def relay_foci(event: dict | None) -> None:
        """Publish FOCI state on change, at most every 15 s while it holds."""
        now = time.monotonic()
        connected = event is not None
        label = event.get("state_label") if event else None
        if (connected, label) == (foci_relay["connected"], foci_relay["label"]) and now - foci_relay["at"] < 15:
            return
        foci_relay.update(connected=connected, label=label, at=now)
        payload: dict = {"connected": connected}
        if event:
            payload.update(state_label=label, focus_depth=event.get("focus_depth"))
        publish_json(TOPIC_FOCI, payload, retain=True)

    # EN: "Hey Specter" (also "Hi Specter") starts a conversation at any time, even with
    # the cover closed: calling him is deliberate. Recognition runs on this computer.
    # 中文：说 "Hey Specter"（"Hi Specter" 也行）随时开始对话，盖子关着也可以，因为这是主动叫他；识别在本机进行。
    wake_enabled = os.getenv("SPECTER_WAKE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if wake_enabled and not dry_run:
        from wake_word import WakeListener

        dictation_cache = [0.0, False]

        def wake_should_listen() -> bool:
            # Not during a conversation (he would hear himself), cat alerts, or dictation.
            if session_gate.active_kind is not None or audio_active.is_set():
                return False
            if not dictation_quiet:
                return True
            # Called every 30 ms frame; the registry read is cached for half a second.
            now = time.monotonic()
            if now - dictation_cache[0] > 0.5:
                dictation_cache[:] = [now, dictating_app() is not None]
            return not dictation_cache[1]

        def on_wake(remainder: str) -> None:
            if not start_conversation("wake", wake_text=remainder):
                LOG.info("Wake word ignored; %s session is active", session_gate.active_kind)

        try:
            import sounddevice as sd

            wake_device = audio_device(sd, os.getenv("SPECTER_MIC_NAME", ""), "input")
            WakeListener(on_wake, wake_should_listen, wake_device).start()
        except Exception:
            LOG.exception("Wake word unavailable; greetings and check-ins still work")

    if foci_enabled or situation_enabled:
        # EN: Check-ins stay governed by SPECTER_FOCI_ENABLED; the situation
        # engine only needs the relayed labels.
        # 中文：主动关怀仍由 SPECTER_FOCI_ENABLED 控制；情境引擎只需要转发的状态标签。
        def monitor_foci() -> None:
            asyncio.run(watch_foci(
                on_foci_trigger if foci_enabled else None,
                url=os.getenv("SPECTER_FOCI_URL", "http://127.0.0.1:8765").rstrip("/"),
                on_sample=relay_foci if situation_enabled else None,
            ))

        threading.Thread(target=monitor_foci, name="foci-state-monitor", daemon=True).start()

    def watch_manual_starts():
        while True:
            try:
                request_id = consume_start_request(log_dir)
                if request_id is not None:
                    LOG.info("Manual session start requested (%s)", request_id)
                    start_conversation()
            except Exception:
                LOG.exception("Manual session start failed")
            time.sleep(0.2)

    threading.Thread(target=watch_manual_starts, name="session-control", daemon=True).start()

    def maintain_memory():
        memory_dir = Path(__file__).with_name("memory")
        sessions_path = memory_dir / "sessions.json"
        long_term_path = memory_dir / "long_term.json"
        while True:
            memory_saved.clear()
            try:
                if consolidation_due(sessions_path, long_term_path):
                    count = asyncio.run(consolidate_memory(
                        sessions_path, long_term_path,
                        tracing_disabled=not sdk_tracing_enabled(),
                    ))
                    LOG.info("Consolidated %d session memories", count)
            except Exception:
                LOG.exception("Memory consolidation failed; session summaries retained")
            memory_saved.wait(60)

    threading.Thread(target=maintain_memory, name="memory-consolidation", daemon=True).start()

    if phone_nudges and phone_voice and situation_enabled and not dry_run:
        def prepare_stay_audio() -> None:
            try:
                stay_audio()
                bounce_audio()
            except Exception:
                LOG.exception("Could not record the wind-down or bounce lines; will retry at the next nudge")

        threading.Thread(target=prepare_stay_audio, name="stay-audio-prep", daemon=True).start()

    def phone_reachable() -> bool:
        if phone_link is None:
            return False
        age = phone_link.status()["sample_age_seconds"]
        return age is not None and age <= 45

    def send_phone_nudge(nudge: dict) -> None:
        """Write the notification (model text or fallback) and queue it for the phone."""
        text = None
        try:
            text = asyncio.run(generate_phone_text(
                nudge, timeout=float(os.getenv("SPECTER_NUDGE_TEXT_TIMEOUT_SECONDS", "8")),
                tracing_disabled=not sdk_tracing_enabled(),
            ))
        except Exception as exc:
            LOG.warning("Phone nudge text unavailable (%s); using the fixed sentence", type(exc).__name__)
        audio_id = stay_audio_id = None
        if phone_voice:
            # EN: Voice is a bonus; the notification still goes out if TTS fails.
            # 中文：语音锦上添花；合成失败时照常发出文字通知。
            spoken = text or phone_fallback_text(nudge)
            try:
                audio_id = phone_link.store_audio(synthesize(spoken))
            except Exception as exc:
                LOG.warning("Phone nudge voice unavailable (%s)", type(exc).__name__)
            try:
                stay_audio_id = phone_link.store_audio(stay_audio())
            except Exception as exc:
                LOG.warning("Stay with me audio unavailable (%s)", type(exc).__name__)
        guard = None
        mode = (nudge.get("context") or {}).get("guard")
        if mode:
            # EN: Levels 3 and 4 lock the phone until morning; the bounce lines are a bonus.
            # 中文：第 3、4 级把手机封锁到早上；拦截语音失败也照样封锁。
            clip_ids = []
            try:
                clip_ids = [phone_link.store_audio(clip) for clip in bounce_audio()]
            except Exception as exc:
                LOG.warning("Bounce lines unavailable (%s)", type(exc).__name__)
            guard = guard_payload(mode, datetime.now(), situation.settings.morning, clip_ids)
        try:
            phone_link.enqueue("nudge", phone_nudge_payload(nudge, text, audio_id, stay_audio_id, guard))
        except ValueError:
            LOG.exception("Could not queue phone nudge")
            publish_json(TOPIC_NUDGE_RESULT, {"id": nudge["id"], "outcome": "failed"})
            return
        LOG.info("Phone nudge %s (level %d) queued with %s text%s%s",
                 nudge["rule"], nudge["level"], "generated" if text else "fixed",
                 " and voice" if audio_id else "", f", lockdown {guard['mode']}" if guard else "")

    def report_situation(decision: Decision, log_path: Path) -> None:
        record = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": decision.kind, "rule": decision.rule, "reason": decision.reason,
        }
        if decision.nudge is not None:
            nudge = decision.nudge.to_payload()
            voiced = situation_voice and not dry_run and nudge["channel"] in voice_channels
            phoned = (
                not voiced and phone_nudges and not dry_run
                and nudge["rule"] in phone_rules and phone_reachable()
            )
            delivery = "voice" if voiced else "phone" if phoned else "silent"
            record.update(nudge, silent=delivery == "silent", delivery=delivery)
            publish_json(TOPIC_NUDGE_REQUEST, {**nudge, "silent": delivery == "silent", "delivery": delivery})
            if phoned:
                # EN: The phone's ack reports delivery; its button or later behavior gives the outcome.
                # 中文：手机确认显示后才算送达；按钮或之后的行为给出结果。
                threading.Thread(target=send_phone_nudge, args=(nudge,), name="phone-nudge", daemon=True).start()
            elif voiced:
                started = start_conversation("nudge", nudge=nudge)
                record["voice_session"] = "started" if started else "skipped_busy"
                publish_json(TOPIC_NUDGE_RESULT, {
                    "id": nudge["id"], "outcome": "delivered" if started else "skipped_busy",
                })
                LOG.info("Voice nudge %s (level %d): %s", nudge["rule"], nudge["level"],
                         "session started" if started else "skipped, another session is active")
            else:
                # EN: Silent mode reports the request as delivered so the budget
                # and repeat spacing behave as they would live.
                # 中文：静默模式按"已送达"回报，使预算与重复间隔按真实情况运转。
                publish_json(TOPIC_NUDGE_RESULT, {
                    "id": nudge["id"], "outcome": "delivered", "await_reply": False,
                })
                LOG.info("SILENT TEST: would nudge %s (level %d, via %s): %s",
                         nudge["rule"], nudge["level"], nudge["channel"], json.dumps(nudge["context"]))
        else:
            LOG.info("Situation %s %s: %s", decision.kind, decision.rule, decision.reason)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            LOG.exception("Could not append %s", log_path.name)

    def run_situation() -> None:
        """Publish desk presence, then evaluate the engine every 15 seconds."""
        log_path = log_dir / "situation.jsonl"
        last_present: bool | None = None
        last_publish = float("-inf")
        LOG.info("Situation engine running (%s)", (
            "; ".join(part for part in (
                f"voice on channels {','.join(sorted(voice_channels))}" if situation_voice else "",
                f"phone for {','.join(sorted(phone_rules))}" if phone_nudges else "",
            ) if part) or "silent test mode"
        ))
        while True:
            try:
                now = time.monotonic()
                with situation_lock:
                    present = owner_presence.present(now)
                    still = owner_presence.still_seconds(now)
                if present != last_present or now - last_publish >= 60:
                    if present != last_present:
                        LOG.info("Owner desk presence: %s", "present" if present else "absent")
                    publish_json(TOPIC_PRESENCE, {
                        "present": present,
                        "still_seconds": round(still) if still is not None else None,
                    }, retain=True)
                    last_present, last_publish = present, now
                with situation_lock:
                    decisions = situation.evaluate(time.monotonic(), datetime.now())
                for decision in decisions:
                    report_situation(decision, log_path)
            except Exception:
                LOG.exception("Situation engine tick failed")
            time.sleep(15)

    if situation is not None:
        threading.Thread(target=run_situation, name="situation-engine", daemon=True).start()

    def restart_camera_bridge(force: bool) -> None:
        """Start the camera bridge task again (end it first when it is stuck)."""
        task = os.getenv("SPECTER_CAMERA_BRIDGE_TASK", "SpecterSaysHiCameraBridge")
        if force:
            subprocess.run(["schtasks", "/End", "/TN", task], capture_output=True, timeout=30)
        result = subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True, timeout=30)
        LOG.warning("Camera offline; %s camera bridge task (exit %d)",
                    "restarted" if force else "started", result.returncode)

    def watch_camera() -> None:
        """Every 2 s: track the cover; heal the camera bridge when frames stop.

        中文：每 2 秒判断盖子开关；画面中断太久就重启摄像头桥接。
        """
        source = frame_source or FrigateFrameSource(
            os.getenv("SPECTER_FRIGATE_URL", "http://127.0.0.1:15000"), gate.camera_name,
        )
        offline_since: float | None = None
        last_heal = float("-inf")
        heals = 0
        while True:
            now = time.monotonic()
            try:
                brightness = frame_brightness(source.latest_jpeg())
                if offline_since is not None:
                    LOG.info("Camera frames are back")
                offline_since, heals = None, 0
                changed = cover.observe(brightness, now)
                if changed is not None:
                    LOG.info("Camera cover %s (brightness %.0f)%s", "closed" if changed else "opened",
                             brightness, ": do not disturb" if changed and cover_dnd else "")
            except VisualUnavailable:
                offline_since = offline_since if offline_since is not None else now
                # EN: The bridge died silently for hours once; start it after 2 minutes,
                # and end-and-restart it if it is running but still delivering nothing.
                # 中文：桥接曾无声退出数小时；离线 2 分钟后启动它，仍无画面就结束后重启。
                if now - offline_since >= 120 and now - last_heal >= 300 and not dry_run:
                    try:
                        restart_camera_bridge(force=heals >= 1)
                    except Exception:
                        LOG.exception("Could not restart the camera bridge")
                    last_heal, heals = now, heals + 1
            except Exception:
                LOG.exception("Camera watch failed")
            time.sleep(2)

    threading.Thread(target=watch_camera, name="camera-watch", daemon=True).start()

    def check_cat(candidate, frame=None, source_kind="frigate"):
        """Identify once and play a single sentence for a clear cat arrival."""
        with cat_worker_lock:
            announced = False
            try:
                if dry_run:
                    LOG.info("DRY RUN: would check cat %s", candidate.object_id)
                    return
                with cat_lock:
                    already_announced = tiled_gate is not None and tiled_gate.announced_encounter
                    local_cat_already_present = (
                        source_kind == "frigate" and tiled_gate is not None and tiled_gate.present
                    )
                # EN: The tiled scan knows whether a cat was already on the sofa
                # at startup. Frigate can re-track that resident cat on movement;
                # such a new ID is not a new arrival.
                # 中文：启动时已经在沙发上的猫不是新到场；Frigate 后续换 ID 也不应播报。
                if already_announced or local_cat_already_present:
                    return
                vision_identity = cat_identifier.classify(candidate, frame)
                classifier = None
                if source_kind == "frigate" and vision_identity in {"tabo", "chichi"}:
                    with cat_lock:
                        classifier_event = cat_gate.classifier_event(candidate.object_id)
                    if classifier_event is not None:
                        classifier_event.wait(timeout=2.0)
                    with cat_lock:
                        classifier = cat_gate.classifier_result(candidate.object_id)
                identity = resolve_cat_identity(vision_identity, classifier)
                LOG.info("Cat identity: vision=%s Frigate=%s final=%s",
                         vision_identity, classifier[0] if classifier else "unavailable", identity)
                with cat_lock:
                    source_present = (
                        candidate.object_id in cat_gate.tracks if source_kind == "frigate"
                        else tiled_gate is not None and tiled_gate.present
                    )
                    can_announce = (
                        source_present and identity != "none"
                        and time.monotonic() - last_cat_announcement[0] >= cat_gate.cooldown_seconds
                    )
                if can_announce:
                    phrase = {
                        "tabo": "Tabo 过来了。",
                        "chichi": "chichi 过来了。",
                        "cat": "猫来了。",
                    }[identity]
                    if cat_play_audio:
                        cat_announcer.speak(identity)
                    else:
                        LOG.info("SILENT TEST: cat confirmed; would say %s", phrase)
                    announced = True
                    with cat_lock:
                        last_cat_announcement[0] = time.monotonic()
                        if tiled_gate is not None:
                            tiled_gate.mark_alert()
                elif identity == "none":
                    LOG.info("Cat candidate rejected by visual check; no alert")
            except Exception:
                LOG.exception("Cat one-shot check failed")
            finally:
                if source_kind == "frigate":
                    with cat_lock:
                        cat_gate.complete(candidate.object_id, announced)

    def scan_cats():
        """Use Frigate frames, but zoom locally so small cats remain detectable."""
        interval = max(1.0, float(os.getenv("SPECTER_CAT_SCAN_INTERVAL_SECONDS", "2")))
        LOG.info("Local tiled cat scan running every %.1f seconds", interval)
        camera_error = ""
        last_unexpected_error_log = float("-inf")
        suppressed_errors = 0
        while True:
            try:
                frame = cat_frame_source.latest_frame()
                detected = tiled_detector.detect(frame)
                with cat_lock:
                    candidate = tiled_gate.observe(detected[0] if detected else None)
                if candidate is not None:
                    LOG.info("Local cat arrival candidate (score %.2f)", detected[1])
                    threading.Thread(
                        target=check_cat, args=(candidate, frame, "tile"),
                        name="cat-tile-check", daemon=True,
                    ).start()
                if camera_error or suppressed_errors:
                    LOG.info("Local tiled cat scan recovered")
                camera_error = ""
                suppressed_errors = 0
            except VisualUnavailable as exc:
                if str(exc) != camera_error:
                    LOG.warning("Local tiled cat scan waiting for camera: %s", exc)
                    camera_error = str(exc)
            except Exception:
                now = time.monotonic()
                if now - last_unexpected_error_log >= 300:
                    LOG.exception(
                        "Local tiled cat scan failed (%d repeated errors suppressed)",
                        suppressed_errors,
                    )
                    last_unexpected_error_log = now
                    suppressed_errors = 0
                else:
                    suppressed_errors += 1
            time.sleep(interval)

    if tiled_detector is not None:
        threading.Thread(target=scan_cats, name="cat-scan", daemon=True).start()

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            LOG.error("MQTT connect failed: %s", reason_code)
            return
        for topic in ("frigate/events", "frigate/tracked_object_update"):
            _client.subscribe(topic)
        if situation is not None:
            for topic in (*SITUATION_TOPICS, f"frigate/{gate.camera_name}/motion"):
                _client.subscribe(topic)
        # EN: The broker keeps no persistence; restate the retained session state.
        # 中文：MQTT 代理不做持久化；重连后重新发布保留的会话状态。
        publish_session_state(session_gate.active_kind)
        LOG.info("Watching Frigate for %s on %s (visual context: %s)",
                 target, gate.camera_name, "enabled" if frame_source else "disabled")
        if cat_enabled:
            LOG.info("One-shot Tabo/chichi arrival alert enabled on %s (audio: %s)",
                     gate.camera_name, "on" if cat_play_audio else "silent test")
        if phone_link is not None:
            current = phone_link.status()
            _client.publish("specter/phone/signal", json.dumps({
                "signal": current["signal"],
                "emitted_at_ms": int(time.time() * 1000),
            }), retain=True)

    def on_message(_client, _userdata, message):
        if owner_presence is not None and message.topic == f"frigate/{gate.camera_name}/motion":
            # EN: Frigate's motion topic is plain ON/OFF text, not JSON.
            # 中文：Frigate 的画面运动话题是纯文本 ON/OFF，不是 JSON。
            with situation_lock:
                owner_presence.observe_motion(message.payload.decode("ascii", "replace"))
            return
        try:
            payload = json.loads(message.payload)
            if owner_presence is not None and message.topic.startswith("frigate/"):
                with situation_lock:
                    owner_presence.observe(message.topic, payload)
            if situation is not None and message.topic in SITUATION_TOPICS:
                with situation_lock:
                    situation.observe(message.topic, payload, time.monotonic())
                return
            if cat_gate is not None and message.topic == "frigate/tracked_object_update":
                with cat_lock:
                    cat_gate.observe_classification(payload)
            if cat_gate is not None and message.topic == "frigate/events":
                with cat_lock:
                    candidate = cat_gate.observe(payload)
                if candidate is not None:
                    threading.Thread(
                        target=check_cat, args=(candidate,), name="cat-alert", daemon=True,
                    ).start()
            if proactive_greeting_enabled and gate.observe(message.topic, payload):
                # EN: Someone already at the desk when the listener (re)starts has not
                # just arrived; restarting must not trigger a greeting.
                # 中文：监听程序（重新）启动时人本来就在桌前，不算刚到，不打招呼。
                reason = (
                    "already present when the listener started"
                    if time.monotonic() - listener_started < 90 else busy_reason()
                )
                if reason:
                    LOG.info("Recognized nearby owner; greeting skipped: %s", reason)
                else:
                    LOG.info("Recognized nearby owner; starting conversation")
                    start_conversation()
        except (ValueError, TypeError):
            LOG.warning("Ignored malformed Frigate event")

    client.on_connect = on_connect
    client.on_message = on_message
    if os.getenv("SPECTER_PHONE_LINK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}:
        def phone_transition(signal: str, sample: dict) -> None:
            LOG.info("Phone use signal: %s (app: %s)", signal, sample.get("foreground_package") or "unknown")
            client.publish("specter/phone/signal", json.dumps({
                "signal": signal,
                "observed_at_ms": sample.get("observed_at_ms"),
                "emitted_at_ms": int(time.time() * 1000),
            }), retain=True)

        tamper_noted = [False]

        def phone_sample(signal: str, sample: dict) -> None:
            client.publish("specter/phone/status", json.dumps({"signal": signal, **sample}))
            # EN: A lockdown whose enforcement service was switched off on the phone.
            # 中文：封锁期间手机上的封锁服务被关掉了：记录下来，留给早上算账。
            tampered = sample.get("guard_mode", "off") != "off" and sample.get("guard_enabled") is False
            if tampered and not tamper_noted[0]:
                LOG.warning("Phone lockdown is active but its enforcement service is switched off")
            tamper_noted[0] = tampered

        def phone_guard_event(event: dict) -> None:
            LOG.info("Phone bounced %s during lockdown", event.get("label") or event["package"])
            publish_json(TOPIC_GUARD, event)

        def phone_ack(command) -> None:
            if command.name == "nudge":
                LOG.info("Phone showed nudge %s", command.payload["nudge_id"])
                publish_json(TOPIC_NUDGE_RESULT, {"id": command.payload["nudge_id"], "outcome": "delivered"})

        def phone_expired(command) -> None:
            if command.name == "nudge":
                LOG.warning("Phone did not fetch nudge %s in time", command.payload["nudge_id"])
                publish_json(TOPIC_NUDGE_RESULT, {"id": command.payload["nudge_id"], "outcome": "failed"})

        def phone_nudge_response(response: dict) -> None:
            # EN: "Stay with me" means going to sleep with his wind-down playing.
            # 中文："Stay with me" 表示听着他的放松引导去睡，算作接受。
            outcome = {
                "accept": "accepted", "snooze": "snoozed", "decline": "declined", "stay": "accepted",
            }[response["action"]]
            LOG.info("Phone nudge reply: %s%s (%s)", outcome,
                     " with Stay with me" if response["action"] == "stay" else "", response["nudge_id"])
            publish_json(TOPIC_NUDGE_RESULT, {
                "id": response["nudge_id"], "outcome": outcome,
                "snooze_minutes": response.get("snooze_minutes"),
                "detail": "stay" if response["action"] == "stay" else "",
            })

        phone_link, _ = start_phone_link(
            on_transition=phone_transition, on_sample=phone_sample,
            on_ack=phone_ack, on_expired=phone_expired, on_nudge_response=phone_nudge_response,
            on_guard_event=phone_guard_event,
        )
    client.connect_async(os.getenv("SPECTER_MQTT_HOST", "127.0.0.1"), int(os.getenv("SPECTER_MQTT_PORT", "1884")))
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()

