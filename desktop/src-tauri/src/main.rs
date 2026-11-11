// ReadmeMonitor desktop shell.
//
// Starts the bundled engine (`rm-engine desktop-serve`, a frozen Python program in the app's
// resources), reads its ready line from a private stdout pipe, signs the app's own window in with
// the one-time code from that line (POST /auth from the loopback page; never in a URL, a command
// line, a log or on disk) and shows the existing interface. The same pipe carries tray commands.
// The loopback page gets no Tauri IPC (no capability lists it), and navigation away from it opens
// the system browser instead. Scheduled checks belong to the OS scheduler: closing the window or
// quitting never cancels them; "Pause background checks" does, deliberately.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::UpdaterExt;
use tauri_plugin_window_state::{StateFlags, WindowExt};

const MAIN: &str = "main";
const READY_TIMEOUT: Duration = Duration::from_secs(60);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

// Links that ask for a new tab become ordinary navigations, which on_navigation hands to the system browser.
const LINK_SCRIPT: &str = r#"(function () {
  function external(e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    if (a.target === "_blank" || e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) {
      e.preventDefault();
      window.location.href = a.href;
    }
  }
  document.addEventListener("click", external, true);
  document.addEventListener("auxclick", external, true);
  window.open = function (u) { if (u) window.location.href = new URL(u, window.location.href).href; return null; };
})();"#;

#[derive(Default)]
struct Helper {
    child: Option<Child>,
    stdin: Option<ChildStdin>,
    port: Option<u16>,
    code: Option<String>,
    version: String,
}

struct Shared {
    helper: Mutex<Helper>,
    pending: Mutex<HashMap<u64, mpsc::Sender<Value>>>,
    next_id: AtomicU64,
    generation: AtomicU64,
    restarts: AtomicU32,
    quitting: AtomicBool,
    updating: AtomicBool,
    tray: Mutex<Option<(MenuItem<Wry>, MenuItem<Wry>)>>,
    has_tray: AtomicBool,
    log: PathBuf,
}

type State = Arc<Shared>;

fn shared(app: &AppHandle) -> State {
    app.state::<State>().inner().clone()
}

// ---- engine helper ------------------------------------------------------------------------

fn engine_path(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Ok(p) = std::env::var("RM_ENGINE") {
        return Ok(PathBuf::from(p)); // development only: a release build always runs its own bundled engine
    }
    let name = if cfg!(windows) { "rm-engine.exe" } else { "rm-engine" };
    let dir = app.path().resource_dir().map_err(|e| format!("resource folder: {e}"))?;
    let exe = dir.join("engine").join(name);
    if exe.is_file() {
        Ok(exe)
    } else {
        Err(format!("The engine is missing from this installation ({}). Reinstall ReadmeMonitor.", exe.display()))
    }
}

fn start_helper(app: &AppHandle) -> Result<(), String> {
    let st = shared(app);
    let exe = engine_path(app)?;
    let mut cmd = Command::new(&exe);
    cmd.arg("desktop-serve");
    #[cfg(debug_assertions)]
    if std::env::var("RM_DEMO").is_ok() {
        cmd.arg("--demo");
    }
    cmd.current_dir(exe.parent().unwrap_or(&exe))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let mut child = cmd.spawn().map_err(|e| format!("Could not start the engine ({e})."))?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take().ok_or("no stderr")?;
    let stdin = child.stdin.take().ok_or("no stdin")?;
    let gen = st.generation.fetch_add(1, Ordering::SeqCst) + 1;

    // The engine's stderr carries only its redacted log output (warnings and errors).
    let log = st.log.clone();
    thread::spawn(move || {
        if let Some(dir) = log.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&log).ok();
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            if let Some(f) = f.as_mut() {
                let _ = writeln!(f, "{line}");
            }
        }
    });

    let (ready_tx, ready_rx) = mpsc::channel::<Value>();
    let app2 = app.clone();
    thread::spawn(move || {
        let st = shared(&app2);
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Ok(v) = serde_json::from_str::<Value>(&line) else { continue };
            if v.get("event").and_then(Value::as_str) == Some("ready") {
                let _ = ready_tx.send(v);
            } else if let Some(id) = v.get("id").and_then(Value::as_u64) {
                if let Some(tx) = st.pending.lock().unwrap().remove(&id) {
                    let _ = tx.send(v);
                }
            }
        }
        helper_exited(&app2, gen);
    });

    let ready = ready_rx
        .recv_timeout(READY_TIMEOUT)
        .map_err(|_| "The engine did not report ready within a minute.".to_string());
    let ready = match ready {
        Ok(r) => r,
        Err(e) => {
            let _ = child.kill();
            return Err(e);
        }
    };
    let port = ready["port"].as_u64().filter(|p| *p > 0 && *p < 65536).ok_or("The engine reported no port.")? as u16;
    let instance = ready["instance"].as_str().unwrap_or_default().to_string();
    // The port came from our own child's pipe; confirm that this listener is that installation.
    if !answers_as(port, &instance) {
        let _ = child.kill();
        return Err("The engine's port did not answer as this installation.".into());
    }
    let mut h = st.helper.lock().unwrap();
    h.child = Some(child);
    h.stdin = Some(stdin);
    h.port = Some(port);
    h.code = ready["code"].as_str().map(str::to_string);
    h.version = ready["version"].as_str().unwrap_or_default().to_string();
    drop(h);
    if let Some(u) = ready.get("update").filter(|u| !u.is_null()) {
        if u["ok"] == json!(false) {
            let msg = format!(
                "The last update could not finish its checks, so scheduled checks stay paused for updates and your data is unchanged.\n\n{}\n\nKeep this version installed and report the problem; do not reinstall an older version.",
                u["error"].as_str().unwrap_or("unknown error")
            );
            notify(app, "Update incomplete", &msg, MessageDialogKind::Warning);
        }
    }
    Ok(())
}

fn answers_as(port: u16, instance: &str) -> bool {
    let Ok(mut s) = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(3)) else { return false };
    let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
    let req = format!("GET /api/session HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n");
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 4096];
    let n = s.read(&mut buf).unwrap_or(0);
    let head = String::from_utf8_lossy(&buf[..n]);
    !instance.is_empty() && head.contains(&format!("X-RM-Instance: {instance}"))
}

fn request(app: &AppHandle, cmd: Value) -> Result<Value, String> {
    let st = shared(app);
    let id = st.next_id.fetch_add(1, Ordering::SeqCst) + 1;
    let (tx, rx) = mpsc::channel();
    st.pending.lock().unwrap().insert(id, tx);
    let mut msg = cmd;
    msg["id"] = json!(id);
    {
        let mut h = st.helper.lock().unwrap();
        let stdin = h.stdin.as_mut().ok_or("The engine is not running.")?;
        writeln!(stdin, "{msg}").and_then(|_| stdin.flush()).map_err(|e| format!("The engine is not reachable ({e})."))?;
    }
    rx.recv_timeout(REQUEST_TIMEOUT).map_err(|_| {
        st.pending.lock().unwrap().remove(&id);
        "The engine did not answer.".to_string()
    })
}

fn stop_helper(app: &AppHandle) {
    let st = shared(app);
    let _ = request(app, json!({"cmd": "quit"}));
    let child = {
        let mut h = st.helper.lock().unwrap();
        h.stdin = None; // EOF: the engine stops serving even if "quit" was not read
        h.port = None;
        h.child.take()
    };
    if let Some(mut c) = child {
        let end = Instant::now() + Duration::from_secs(10);
        while Instant::now() < end {
            if matches!(c.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
        let _ = c.kill(); // only the interface server; checks it launched run in their own processes
        let _ = c.wait();
    }
}

fn helper_exited(app: &AppHandle, gen: u64) {
    let st = shared(app);
    if st.generation.load(Ordering::SeqCst) != gen || st.quitting.load(Ordering::SeqCst) || st.updating.load(Ordering::SeqCst) {
        return;
    }
    {
        let mut h = st.helper.lock().unwrap();
        h.stdin = None;
        h.port = None;
        if let Some(mut c) = h.child.take() {
            let _ = c.wait();
        }
    }
    let n = st.restarts.fetch_add(1, Ordering::SeqCst);
    if n >= 3 {
        show_splash(app, &format!("failed:The background helper stopped repeatedly. Its log is in {}", st.log.display()));
        return;
    }
    show_splash(app, "reconnecting");
    thread::sleep(Duration::from_secs([1, 3, 10][n as usize]));
    connect(app.clone());
}

/// Start the engine (if needed) and sign the window in. Runs off the main thread.
fn connect(app: AppHandle) {
    thread::spawn(move || {
        let running = shared(&app).helper.lock().unwrap().port.is_some();
        let result = if running { Ok(()) } else { start_helper(&app) };
        match result {
            Ok(()) => {
                let port = shared(&app).helper.lock().unwrap().port;
                if let (Some(port), Some(w)) = (port, app.get_webview_window(MAIN)) {
                    let _ = w.navigate(Url::parse(&format!("http://127.0.0.1:{port}/desktop-auth")).unwrap());
                }
            }
            Err(e) => show_splash(&app, &format!("failed:{e}\n\nDetails: {}", shared(&app).log.display())),
        }
    });
}

fn show_splash(app: &AppHandle, state: &str) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let base = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
        let mut url = Url::parse(base).unwrap();
        url.set_fragment(Some(state));
        let _ = w.navigate(url);
    }
}

fn is_shell_page(url: &Url) -> bool {
    url.scheme() == "tauri" || url.host_str() == Some("tauri.localhost")
}

fn sign_in(app: &AppHandle, w: &WebviewWindow) {
    let code = shared(app).helper.lock().unwrap().code.take();
    let code = match code {
        Some(c) => c,
        None => match request(app, json!({"cmd": "sign_in"})) {
            Ok(v) => v["code"].as_str().unwrap_or_default().to_string(),
            Err(e) => {
                show_splash(app, &format!("failed:{e}"));
                return;
            }
        },
    };
    let js = format!(
        "fetch('/auth',{{method:'POST',headers:{{'X-RM-Pair':{}}}}}).then(function(r){{location.replace(r.ok?'/':'/desktop-auth?failed=1')}},function(){{location.replace('/desktop-auth?failed=1')}});",
        serde_json::to_string(&code).unwrap()
    );
    let _ = w.eval(js);
}

// ---- window, tray and menus ----------------------------------------------------------------

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn build_window(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    let nav_app = app.clone();
    let load_app = app.clone();
    let w = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html#starting".into()))
        .title("ReadmeMonitor")
        .inner_size(1280.0, 860.0)
        .min_inner_size(720.0, 520.0)
        .visible(false)
        .initialization_script(LINK_SCRIPT)
        .on_navigation(move |url| {
            if is_shell_page(url) {
                if url.query() == Some("retry=1") {
                    shared(&nav_app).restarts.store(0, Ordering::SeqCst);
                    connect(nav_app.clone());
                }
                return true;
            }
            let port = shared(&nav_app).helper.lock().unwrap().port;
            if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port().is_some() && url.port() == port {
                return true;
            }
            if matches!(url.scheme(), "https" | "http" | "mailto") {
                let _ = nav_app.opener().open_url(url.as_str(), None::<&str>); // the system browser, never this window
            }
            false
        })
        .on_page_load(move |w, payload| {
            if payload.event() != PageLoadEvent::Finished {
                return;
            }
            let url = payload.url().clone();
            if url.host_str() == Some("127.0.0.1") && url.path() == "/desktop-auth" {
                let app = load_app.clone();
                if url.query() == Some("failed=1") {
                    show_splash(&app, "failed:The window could not sign in to the background helper. Try again.");
                } else {
                    thread::spawn(move || sign_in(&app, &w));
                }
            }
        })
        .build()?;
    let _ = w.restore_state(StateFlags::all() & !StateFlags::VISIBLE);
    w.show()?;
    Ok(w)
}

fn build_menus(app: &AppHandle) -> tauri::Result<()> {
    let quit = MenuItem::with_id(app, "quit", "Quit ReadmeMonitor", true, Some("CmdOrCtrl+Q"))?;
    let updates = MenuItem::with_id(app, "updates", "Check for updates…", true, None::<&str>)?;
    let about = MenuItem::with_id(app, "about", "About ReadmeMonitor", true, None::<&str>)?;
    let sep = || PredefinedMenuItem::separator(app);
    let file = Submenu::with_items(app, "File", true, &[&PredefinedMenuItem::close_window(app, Some("Close window"))?, &sep()?, &quit])?;
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &sep()?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    let view = Submenu::with_items(app, "View", true, &[&MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?])?;
    let help = Submenu::with_items(app, "Help", true, &[&updates, &sep()?, &about])?;
    #[cfg(target_os = "macos")]
    let menu = Menu::with_items(
        app,
        &[
            &Submenu::with_items(app, "ReadmeMonitor", true, &[&about, &updates, &sep()?, &PredefinedMenuItem::hide(app, None)?, &quit])?,
            &file,
            &edit,
            &view,
        ],
    )?;
    #[cfg(not(target_os = "macos"))]
    let menu = Menu::with_items(app, &[&file, &edit, &view, &help])?;
    let _ = &help;
    app.set_menu(menu)?;

    // Tray / menu bar. On Linux the indicator may be invisible (GNOME), so there closing quits.
    let open = MenuItem::with_id(app, "open", "Open ReadmeMonitor", true, None::<&str>)?;
    let run = MenuItem::with_id(app, "run", "Run a check now", false, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause background checks", false, None::<&str>)?;
    let tray_menu = Menu::with_items(
        app,
        &[&open, &sep()?, &run, &pause, &sep()?, &MenuItem::with_id(app, "updates", "Check for updates…", true, None::<&str>)?, &sep()?, &MenuItem::with_id(app, "quit", "Quit ReadmeMonitor", true, None::<&str>)?],
    )?;
    let tray = TrayIconBuilder::with_id("tray")
        .icon(app.default_window_icon().cloned().expect("bundle icon"))
        .tooltip("ReadmeMonitor")
        .menu(&tray_menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                show_main(tray.app_handle());
            }
        })
        .build(app);
    let st = shared(app);
    st.has_tray.store(tray.is_ok() && !cfg!(target_os = "linux"), Ordering::SeqCst);
    *st.tray.lock().unwrap() = Some((run, pause));
    Ok(())
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "open" => show_main(app),
        "quit" => quit(app.clone()),
        "reload" => {
            if let Some(w) = app.get_webview_window(MAIN) {
                let _ = w.eval("location.reload()");
            }
        }
        "about" => {
            let app = app.clone();
            thread::spawn(move || about(&app));
        }
        "updates" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move { check_updates(app, true).await });
        }
        "run" => {
            let app = app.clone();
            thread::spawn(move || match request(&app, json!({"cmd": "run"})) {
                Ok(v) if v["started"] == json!(true) => {}
                Ok(v) => {
                    let why = match v["reason"].as_str().unwrap_or("") {
                        "already_running" => "A check is already running.",
                        "inactive" => "The worker is not enabled on this computer yet. Open ReadmeMonitor to finish setup.",
                        "paused" => "Background checks are paused. Resume them first.",
                        "updating" => "An application update is being installed.",
                        "credentials_missing" => "Add a GitHub credential in ReadmeMonitor first.",
                        _ => "The check did not start.",
                    };
                    notify(&app, "Run a check now", why, MessageDialogKind::Info);
                }
                Err(e) => notify(&app, "Run a check now", &e, MessageDialogKind::Error),
            });
        }
        "pause" => {
            let app = app.clone();
            thread::spawn(move || {
                let paused = request(&app, json!({"cmd": "state"})).map(|v| v["paused"] == json!(true)).unwrap_or(false);
                let _ = request(&app, json!({"cmd": if paused { "resume" } else { "pause" }}));
                refresh_tray(&app);
            });
        }
        _ => {}
    }
}

fn refresh_tray(app: &AppHandle) {
    let st = shared(app);
    let Some((run, pause)) = st.tray.lock().unwrap().clone() else { return };
    match request(app, json!({"cmd": "state"})) {
        Ok(s) => {
            let (active, paused, busy, updating) = (s["active"] == json!(true), s["paused"] == json!(true), s["busy"] == json!(true), s["updating"] == json!(true));
            let _ = run.set_enabled(active && !paused && !busy && !updating);
            let _ = run.set_text(if busy { "A check is running…" } else { "Run a check now" });
            let _ = pause.set_enabled(true);
            let _ = pause.set_text(if paused { "Resume background checks" } else { "Pause background checks" });
            let tip = if updating { "updating" } else if busy { "checking now" } else if paused { "background checks paused" } else if active { "ready" } else { "setup not finished" };
            if let Some(t) = app.tray_by_id("tray") {
                let _ = t.set_tooltip(Some(format!("ReadmeMonitor: {tip}")));
            }
        }
        Err(_) => {
            let _ = run.set_enabled(false);
            let _ = pause.set_enabled(false);
        }
    }
}

fn notify(app: &AppHandle, title: &str, text: &str, kind: MessageDialogKind) {
    app.dialog().message(text).title(title).kind(kind).show(|_| {});
}

fn about(app: &AppHandle) {
    let engine = shared(app).helper.lock().unwrap().version.clone();
    let licences = app.path().resource_dir().map(|d| d.join("licenses").display().to_string()).unwrap_or_default();
    let text = format!(
        "ReadmeMonitor {}\nEngine {}\n\nApache-2.0. ReadmeMonitor and the third-party licences it ships are in:\n{}",
        app.package_info().version,
        if engine.is_empty() { "not running".into() } else { engine },
        licences
    );
    notify(app, "About ReadmeMonitor", &text, MessageDialogKind::Info);
}

fn first_close_notice(app: &AppHandle) {
    let Ok(dir) = app.path().app_config_dir() else { return };
    let mark = dir.join("close-notice-shown");
    if mark.exists() {
        return;
    }
    let _ = std::fs::create_dir_all(&dir);
    let _ = std::fs::write(&mark, b"1");
    notify(
        app,
        "ReadmeMonitor keeps running",
        "The window is closed; ReadmeMonitor stays available from its tray icon. Scheduled checks run whether or not the app is open. To stop them, use \"Pause background checks\". To close the app, use Quit.",
        MessageDialogKind::Info,
    );
}

fn quit(app: AppHandle) {
    let st = shared(&app);
    if st.quitting.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || {
        stop_helper(&app);
        app.exit(0);
    });
}

// ---- updates ---------------------------------------------------------------------------------

async fn check_updates(app: AppHandle, interactive: bool) {
    let st = shared(&app);
    if st.updating.load(Ordering::SeqCst) {
        return;
    }
    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            if interactive {
                notify(&app, "Check for updates", &format!("Updates are not available in this build: {e}"), MessageDialogKind::Warning);
            }
            return;
        }
    };
    match updater.check().await {
        Ok(Some(update)) => {
            let mut notes = update.body.clone().unwrap_or_default();
            if notes.chars().count() > 1500 {
                notes = notes.chars().take(1500).collect::<String>() + "…";
            }
            let text = format!(
                "ReadmeMonitor {} is available (this is {}).\n\n{}\n\nA check that is running finishes first. Your settings, history, spending records, schedule and credentials are kept.",
                update.version, update.current_version, notes
            );
            let app2 = app.clone();
            app.dialog()
                .message(text)
                .title("Update available")
                .buttons(MessageDialogButtons::OkCancelCustom("Install and restart".into(), "Later".into()))
                .show(move |yes| {
                    if yes {
                        tauri::async_runtime::spawn(async move { install_update(app2, update).await });
                    }
                });
        }
        Ok(None) => {
            if interactive {
                notify(&app, "Check for updates", &format!("ReadmeMonitor {} is the latest version.", app.package_info().version), MessageDialogKind::Info);
            }
        }
        Err(e) => {
            if interactive {
                notify(&app, "Check for updates", &format!("Could not check for updates: {e}"), MessageDialogKind::Error);
            }
        }
    }
}

fn set_title(app: &AppHandle, title: &str) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.set_title(title);
    }
}

async fn install_update(app: AppHandle, update: tauri_plugin_updater::Update) {
    let st = shared(&app);
    if st.updating.swap(true, Ordering::SeqCst) {
        return;
    }
    // 1. Download; the updater verifies the signature against the public key in this app.
    let mut got: u64 = 0;
    let app_p = app.clone();
    let bytes = update
        .download(
            move |chunk, total| {
                got += chunk as u64;
                if let Some(t) = total.filter(|t| *t > 0) {
                    set_title(&app_p, &format!("ReadmeMonitor: downloading update {}%", got * 100 / t));
                }
            },
            || {},
        )
        .await;
    let bytes = match bytes {
        Ok(b) => b,
        Err(e) => {
            st.updating.store(false, Ordering::SeqCst);
            set_title(&app, "ReadmeMonitor");
            notify(&app, "Update not installed", &format!("The update could not be downloaded or verified, so nothing changed.\n\n{e}"), MessageDialogKind::Error);
            return;
        }
    };
    // 2. Wait for any running check, back up the database and set the maintenance flag (engine side, under worker.lock).
    set_title(&app, "ReadmeMonitor: preparing the update…");
    let version = update.version.clone();
    let prepared = {
        let app = app.clone();
        tauri::async_runtime::spawn_blocking(move || -> Result<(), String> {
            let end = Instant::now() + Duration::from_secs(2 * 3600);
            loop {
                let v = request(&app, json!({"cmd": "prepare_update", "version": version}))?;
                if v["ok"] != json!(true) {
                    return Err(v["error"].as_str().unwrap_or("the engine refused").to_string());
                }
                if v["deferred"] != json!(true) {
                    return Ok(());
                }
                if Instant::now() > end {
                    return Err("a check was still running after two hours".into());
                }
                set_title(&app, "ReadmeMonitor: the update waits for the running check to finish…");
                thread::sleep(Duration::from_secs(15));
            }
        })
        .await
        .unwrap_or_else(|e| Err(e.to_string()))
    };
    if let Err(e) = prepared {
        st.updating.store(false, Ordering::SeqCst);
        set_title(&app, "ReadmeMonitor");
        notify(&app, "Update not installed", &format!("Nothing changed: {e}"), MessageDialogKind::Error);
        return;
    }
    // 3. Stop the helper this app owns, then install. Windows runs the installer and restarts the app.
    let app_s = app.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || stop_helper(&app_s)).await;
    match update.install(bytes) {
        Ok(()) => app.restart(),
        Err(e) => {
            // The flag stays until the engine's start-up health check clears it; data is untouched.
            st.updating.store(false, Ordering::SeqCst);
            set_title(&app, "ReadmeMonitor");
            notify(&app, "Update not installed", &format!("The installation failed; this version keeps running.\n\n{e}"), MessageDialogKind::Error);
            connect(app.clone());
        }
    }
}

// ---- entry -------------------------------------------------------------------------------------

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| show_main(app)))
        .plugin(tauri_plugin_window_state::Builder::default().with_state_flags(StateFlags::all() & !StateFlags::VISIBLE).build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .on_menu_event(|app, e| on_menu(app, e.id().as_ref()))
        .setup(|app| {
            let log = app.path().app_log_dir().unwrap_or_else(|_| std::env::temp_dir()).join("helper.log");
            app.manage::<State>(Arc::new(Shared {
                helper: Mutex::new(Helper::default()),
                pending: Mutex::new(HashMap::new()),
                next_id: AtomicU64::new(0),
                generation: AtomicU64::new(0),
                restarts: AtomicU32::new(0),
                quitting: AtomicBool::new(false),
                updating: AtomicBool::new(false),
                tray: Mutex::new(None),
                has_tray: AtomicBool::new(false),
                log,
            }));
            let handle = app.handle().clone();
            build_menus(&handle)?;
            let w = build_window(&handle)?;
            let h2 = handle.clone();
            w.on_window_event(move |e| {
                if let WindowEvent::CloseRequested { api, .. } = e {
                    let st = shared(&h2);
                    if st.has_tray.load(Ordering::SeqCst) && !st.quitting.load(Ordering::SeqCst) {
                        api.prevent_close();
                        if let Some(w) = h2.get_webview_window(MAIN) {
                            let _ = w.hide();
                        }
                        first_close_notice(&h2);
                    } else {
                        quit(h2.clone());
                    }
                }
            });
            connect(handle.clone());
            let h3 = handle.clone();
            thread::spawn(move || {
                let mut ticks = 0u32;
                loop {
                    thread::sleep(Duration::from_secs(5));
                    if shared(&h3).quitting.load(Ordering::SeqCst) {
                        break;
                    }
                    refresh_tray(&h3);
                    ticks += 1;
                    if ticks == 6 || ticks % 17280 == 0 {
                        // 30 s after start, then daily: offer an update if one is published
                        let h = h3.clone();
                        tauri::async_runtime::spawn(async move { check_updates(h, false).await });
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("ReadmeMonitor could not start");
    app.run(|app, event| {
        if let RunEvent::ExitRequested { api, code, .. } = &event {
            // Keep running in the tray when the window closes; an explicit Quit passes through.
            if code.is_none() && shared(app).has_tray.load(Ordering::SeqCst) && !shared(app).quitting.load(Ordering::SeqCst) {
                api.prevent_exit();
            }
        }
    });
}
