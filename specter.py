"""Frigate MQTT -> owner conversations and one-shot cat arrival alerts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from cat_alert import CatArrivalGate, CatIdentifier, OneShotCatAnnouncer, resolve_cat_identity
from foci_state import FociAssessment, watch_foci
from log_retention import RetainedRotatingFileHandler
from mini_audio_publisher import AVATAR_ENV_KEYS
from presence import PresenceGate
from phone_link import start_background as start_phone_link
from realtime_agent import RealtimeConversation, sdk_tracing_enabled
from session_control import consume_start_request
from session_gate import SessionGate
from session_memory import consolidation_due, consolidate_memory
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
    session_gate = SessionGate()
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

    def start_conversation(kind: str = "greeting", assessment: FociAssessment | None = None) -> bool:
        if dry_run:
            LOG.info("DRY RUN: would start %s session", kind)
            return True

        def run() -> None:
            conversation = RealtimeConversation(
                target,
                frame_source=frame_source if kind == "greeting" else None,
                external_audio_active=audio_active,
                memory_saved_event=memory_saved,
                session_kind=kind,
                foci_context=assessment.reason if assessment else "",
            )
            conversation.run()

        started = session_gate.start(kind, run)
        if not started and kind == "greeting":
            LOG.info("%s session skipped; %s session is active", kind, session_gate.active_kind)
        return started

    def on_foci_trigger(assessment: FociAssessment) -> bool:
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
    LOG.info("Proactive triggers: greeting %s, FOCI %s",
             "on" if proactive_greeting_enabled else "off",
             "on" if foci_enabled else "off")
    if foci_enabled:
        def monitor_foci() -> None:
            asyncio.run(watch_foci(
                on_foci_trigger,
                url=os.getenv("SPECTER_FOCI_URL", "http://127.0.0.1:8765").rstrip("/"),
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

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    phone_link = None

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            LOG.error("MQTT connect failed: %s", reason_code)
            return
        for topic in ("frigate/events", "frigate/tracked_object_update"):
            _client.subscribe(topic)
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
        try:
            payload = json.loads(message.payload)
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

        def phone_sample(signal: str, sample: dict) -> None:
            client.publish("specter/phone/status", json.dumps({"signal": signal, **sample}))

        phone_link, _ = start_phone_link(on_transition=phone_transition, on_sample=phone_sample)
    client.connect_async(os.getenv("SPECTER_MQTT_HOST", "127.0.0.1"), int(os.getenv("SPECTER_MQTT_PORT", "1884")))
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()

