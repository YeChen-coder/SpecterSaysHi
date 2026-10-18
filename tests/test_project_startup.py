"""Exercise the real main controller with isolated Docker/task stand-ins."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which('powershell') or shutil.which('pwsh')


@unittest.skipUnless(SHELL, 'PowerShell is required')
class ProjectStartupTests(unittest.TestCase):
    def run_controller(self, action='Start', *, dry_run=False, recreate=False, fail_avatar=False, real_avatar=False, character='QinChe'):
        with tempfile.TemporaryDirectory(prefix='specter-startup-') as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'specter_control.ps1', root / 'specter_control.ps1')
            (root / 'compose.yaml').write_text('name: specter-says-hi\nservices: {}\n')
            (root / 'start_avatar.ps1').write_text(
                "Add-Content -LiteralPath $env:STARTUP_TEST_EVENTS -Value 'avatar'\n"
                + ("throw 'Avatar test failure'\n" if fail_avatar else ''), encoding='utf-8')
            if real_avatar:
                shutil.copyfile(ROOT / 'start_avatar.ps1', root / 'start_avatar.ps1')
                (root / '.env').write_text(f'SPECTER_AVATAR_BACKEND=feathertalk\nSPECTER_AVATAR_CHARACTER={character}\n')
            harness = r'''
param([string]$Action, [switch]$DryRun, [switch]$ForceRecreate)
$ErrorActionPreference = 'Stop'
$env:STARTUP_TEST_EVENTS = Join-Path $PSScriptRoot 'events.txt'
function Get-Command { [pscustomobject]@{Source='Invoke-TestDocker'} }
function Invoke-TestDocker { Add-Content $env:STARTUP_TEST_EVENTS ('docker:' + ($args -join ' ')) }
function docker { Invoke-TestDocker @args }
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    $scriptName = if ($TaskName -eq 'SpecterSaysHiCameraBridge') {'camera_bridge.ps1'} else {'voice_listener.ps1'}
    [pscustomobject]@{TaskName=$TaskName; State='Disabled'; Actions=@([pscustomobject]@{Arguments=(Join-Path $PSScriptRoot $scriptName)})}
}
function Enable-ScheduledTask { param($TaskName); Add-Content $env:STARTUP_TEST_EVENTS "enable:$TaskName" }
function Start-ScheduledTask { param($TaskName); Add-Content $env:STARTUP_TEST_EVENTS "start:$TaskName" }
function Disable-ScheduledTask { param($TaskName); Add-Content $env:STARTUP_TEST_EVENTS "disable:$TaskName" }
function Stop-ScheduledTask { param($TaskName, $ErrorAction); Add-Content $env:STARTUP_TEST_EVENTS "stop:$TaskName" }
function Get-CimInstance { @() }
$global:LASTEXITCODE = 0
& (Join-Path $PSScriptRoot 'specter_control.ps1') -Action $Action -DryRun:$DryRun -ForceRecreate:$ForceRecreate
'''
            (root / 'harness.ps1').write_text(harness, encoding='utf-8')
            args = [SHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(root / 'harness.ps1'), '-Action', action]
            if dry_run:
                args.append('-DryRun')
            if recreate:
                args.append('-ForceRecreate')
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            events_file = root / 'events.txt'
            events = events_file.read_text(encoding='utf-8-sig').splitlines() if events_file.exists() else []
            return result, events

    def test_avatar_starts_after_compose_before_tasks(self):
        result, events = self.run_controller()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(events[0].startswith('docker:compose -p specter-says-hi '))
        self.assertTrue(events[0].endswith(' up -d mediamtx mosquitto frigate'))
        self.assertEqual(events[1], 'avatar')
        self.assertEqual(events[2:], ['enable:SpecterSaysHiCameraBridge', 'start:SpecterSaysHiCameraBridge',
                                     'enable:SpecterSaysHiVoiceListener', 'start:SpecterSaysHiVoiceListener'])

    def test_avatar_failure_prevents_task_activation(self):
        result, events = self.run_controller(fail_avatar=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1], 'avatar')

    def test_real_avatar_script_passes_lab_compose_path_to_docker(self):
        result, events = self.run_controller(real_avatar=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('docker:compose -f ', events[1])
        self.assertTrue(events[1].endswith('feathertalk_lab\\compose.yaml stop loop'), events)
        self.assertTrue(events[2].endswith(' feathertalk'), events)
        self.assertTrue(events[3].endswith(' feathertalk-qinche'), events)
        self.assertEqual(events[4], 'enable:SpecterSaysHiCameraBridge')

    def test_switch_back_to_gentleman_stops_qinche_service(self):
        result, events = self.run_controller(real_avatar=True, character='gentleman')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[2], 'docker:compose stop feathertalk-qinche')
        self.assertTrue(events[3].endswith(' feathertalk'), events)

    def test_dry_run_has_no_side_effects(self):
        result, events = self.run_controller(dry_run=True, recreate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [])
        self.assertIn('Starting the selected avatar', result.stdout)

    def test_recreation_still_applies_selected_avatar(self):
        result, events = self.run_controller(recreate=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[:2], ['stop:SpecterSaysHiVoiceListener', 'stop:SpecterSaysHiCameraBridge'])
        self.assertIn('--force-recreate', events[2])
        self.assertEqual(events[3], 'avatar')

    def test_stop_does_not_start_avatar(self):
        result, events = self.run_controller(action='Stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('avatar', events)
        self.assertTrue(events[-1].endswith(' stop'))
        self.assertIn('--profile * stop', events[-1])


if __name__ == '__main__':
    unittest.main()
