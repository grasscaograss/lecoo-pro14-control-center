mod policy;

#[cfg(windows)]
mod service;

fn main() {
    #[cfg(windows)]
    if let Err(error) = service::dispatch() {
        eprintln!("Fan service failed: {error}");
        std::process::exit(1);
    }
}
