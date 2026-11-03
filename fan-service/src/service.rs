use crate::policy::{Controller, Mode, Policy};
use ipc::{FanIndex, FanMode, IpcClient, IpcRequest, IpcResponse};
use serde_json::json;
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use windows_service::{
    define_windows_service,
    service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    },
    service_control_handler::{self, ServiceControlHandlerResult},
    service_dispatcher,
};

const NAME: &str = "LecooFanControl";
const DATA: &str = "C:\\ProgramData\\LecooControl";
define_windows_service!(ffi_service_main, service_main);

pub fn dispatch() -> windows_service::Result<()> {
    service_dispatcher::start(NAME, ffi_service_main)
}

fn config_path() -> Result<PathBuf, String> {
    let args: Vec<_> = std::env::args_os().collect();
    args.windows(2)
        .find(|pair| pair[0] == "--config")
        .map(|pair| PathBuf::from(&pair[1]))
        .ok_or_else(|| "Missing --config path".into())
}

fn status(state: ServiceState, exit: u32) -> ServiceStatus {
    ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: state,
        controls_accepted: if state == ServiceState::Running {
            ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN
        } else {
            ServiceControlAccept::empty()
        },
        exit_code: ServiceExitCode::Win32(exit),
        checkpoint: 0,
        wait_hint: Duration::from_secs(5),
        process_id: None,
    }
}

fn service_main(_arguments: Vec<std::ffi::OsString>) {
    let _ = fs::create_dir_all(DATA);
    let log_path = Path::new(DATA).join("fan-service.log");
    if fs::metadata(&log_path)
        .map(|m| m.len() > 1024 * 1024)
        .unwrap_or(false)
    {
        let _ = fs::rename(&log_path, log_path.with_extension("log.1"));
    }
    if let Ok(file) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
    {
        let _ = simplelog::WriteLogger::init(
            log::LevelFilter::Info,
            simplelog::Config::default(),
            file,
        );
    }
    let stopped = Arc::new(AtomicBool::new(false));
    let signal = stopped.clone();
    let handle = match service_control_handler::register(NAME, move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown => {
            signal.store(true, Ordering::Relaxed);
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    }) {
        Ok(handle) => handle,
        Err(error) => {
            log::error!("Control handler: {error}");
            return;
        }
    };
    let path = match config_path() {
        Ok(path) => path,
        Err(error) => {
            log::error!("{error}");
            let _ = handle.set_service_status(status(ServiceState::Stopped, 1));
            return;
        }
    };
    log::info!("Starting as system service; config={}", path.display());
    let heartbeat = Arc::new(AtomicU64::new(0));
    let clock = Instant::now();
    let worker_stop = stopped.clone();
    let worker_heartbeat = heartbeat.clone();
    let (done_tx, done_rx) = mpsc::channel();
    std::thread::spawn(move || {
        run(&path, worker_stop, worker_heartbeat, clock);
        let _ = done_tx.send(());
    });
    let _ = handle.set_service_status(status(ServiceState::Running, 0));
    let mut failure = 0;
    loop {
        if stopped.load(Ordering::Relaxed) {
            break;
        }
        match done_rx.recv_timeout(Duration::from_secs(1)) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                failure = 1;
                break;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        if clock
            .elapsed()
            .as_secs()
            .saturating_sub(heartbeat.load(Ordering::Relaxed))
            > 20
        {
            log::error!("Core IPC timed out; exiting for SCM recovery");
            failure = 1;
            break;
        }
    }
    stopped.store(true, Ordering::Relaxed);
    let _ = handle.set_service_status(status(ServiceState::StopPending, failure));
    let _ = done_rx.recv_timeout(Duration::from_secs(2));
    log::info!("Stopped; exit={failure}");
    let _ = handle.set_service_status(status(ServiceState::Stopped, failure));
    // Ending the service process also releases any IPC worker blocked in the core.
    std::process::exit(failure as i32);
}

fn load(path: &Path) -> Result<Policy, String> {
    let raw = fs::read(path).map_err(|error| error.to_string())?;
    if raw.len() > 64 * 1024 {
        return Err("Policy file is too large".into());
    }
    let policy: Policy = serde_json::from_slice(&raw).map_err(|error| error.to_string())?;
    policy.validate()?;
    Ok(policy)
}

fn write_json(path: &Path, value: &impl serde::Serialize) -> std::io::Result<()> {
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, serde_json::to_vec_pretty(value)?)?;
    fs::rename(temporary, path)
}

fn request(client: &mut IpcClient, message: &IpcRequest) -> Result<IpcResponse, String> {
    let response: IpcResponse = client.request(message).map_err(|error| error.to_string())?;
    match response {
        IpcResponse::Error(error) => Err(error),
        response => Ok(response),
    }
}

fn set_fans(client: &mut IpcClient, modes: [Mode; 2]) -> Result<(), String> {
    for (fan, mode) in [FanIndex::Cpu, FanIndex::Gpu].into_iter().zip(modes) {
        let mode = match mode {
            Mode::Auto => FanMode::Auto,
            Mode::Full => FanMode::Full,
            Mode::Custom(pwm) => FanMode::Custom(pwm),
        };
        match request(client, &IpcRequest::SetFanMode { fan, mode })? {
            IpcResponse::Success => {}
            response => return Err(format!("Unexpected fan response: {response:?}")),
        }
    }
    Ok(())
}

fn run(path: &Path, stop: Arc<AtomicBool>, heartbeat: Arc<AtomicU64>, clock: Instant) {
    let snapshot = Path::new(DATA).join("fan-policy.json");
    let status_path = Path::new(DATA).join("fan-service-status.json");
    let mut policy = load(&snapshot).ok();
    let mut controller = Controller::default();
    let mut client = None;
    let mut last_error = String::new();
    let mut config_error = String::new();
    let mut last_modes = None;
    let mut last_status = 0;
    while !stop.load(Ordering::Relaxed) {
        let now = clock.elapsed().as_secs();
        heartbeat.store(now, Ordering::Relaxed);
        match load(path) {
            Ok(updated) if policy.as_ref() != Some(&updated) => {
                log::info!(
                    "Loaded fan policy: curve_enabled={} points={:?} rise={} fall={}",
                    updated.curve_enabled,
                    updated.curve_points,
                    updated.curve_rise_delay,
                    updated.curve_fall_delay
                );
                if let Err(error) = write_json(&snapshot, &updated) {
                    log::error!("Persist fan policy: {error}");
                }
                policy = Some(updated);
                controller.reset();
                config_error.clear();
            }
            Err(error) => {
                if config_error != error {
                    log::warn!("Config unavailable; retaining validated policy: {error}");
                    config_error = error;
                }
            }
            _ => {
                config_error.clear();
            }
        }
        let result = (|| -> Result<(Option<u8>, Option<[Mode; 2]>), String> {
            if client.is_none() {
                client = Some(IpcClient::connect().map_err(|error| error.to_string())?);
                controller.reset();
            }
            let connection = client.as_mut().unwrap();
            let temp = match request(connection, &IpcRequest::GetTemperatures)? {
                IpcResponse::Temp(cpu, _) if (1..=110).contains(&cpu) => Some(cpu),
                IpcResponse::Temp(_, _) => None,
                response => return Err(format!("Unexpected temperature response: {response:?}")),
            };
            let desired = if let Some(policy) = policy.as_ref() {
                controller.plan(policy, temp, now)
            } else {
                Some([Mode::Auto; 2])
            };
            if let Some(modes) = desired {
                set_fans(connection, modes)?;
                controller.committed(modes, temp.unwrap_or(0), now);
                if last_modes != Some(modes) {
                    log::info!("Fan target applied: cpu_temp={temp:?} modes={modes:?}");
                    last_modes = Some(modes);
                }
            }
            Ok((temp, desired))
        })();
        let (temp, connected) = match result {
            Ok((temp, _)) => {
                if !last_error.is_empty() {
                    log::info!("Core IPC ready");
                    last_error.clear();
                }
                (temp, true)
            }
            Err(error) => {
                if error != last_error || now % 30 == 0 {
                    log::warn!("Core IPC unavailable: {error}");
                }
                last_error = error;
                client = None;
                controller.reset();
                (None, false)
            }
        };
        if now.saturating_sub(last_status) >= 5 || now == 0 {
            let _ = write_json(
                &status_path,
                &json!({"pid":std::process::id(),"unix_time":SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs(),"config_path":path,"core_connected":connected,"curve_enabled":policy.as_ref().map(|p|p.curve_enabled).unwrap_or(false),"cpu_temperature":temp,"applied_modes":last_modes,"last_error":last_error}),
            );
            last_status = now;
        }
        for _ in 0..10 {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    if let Some(mut connection) = client {
        let _ = set_fans(&mut connection, [Mode::Auto; 2]);
    }
}
