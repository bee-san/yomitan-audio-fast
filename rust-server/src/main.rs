mod benchmark;
mod bundle;
mod compiler;
mod query;
mod server;
#[cfg(test)]
mod tests;

use std::net::IpAddr;
use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};

use crate::compiler::CompileOptions;
use crate::query::LookupMode;
use crate::server::{AssetMode, ServeOptions};

#[derive(Parser)]
#[command(name = "yomitan-audio-rs", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Compile copied legacy entries.db + source files into an immutable native bundle.
    Compile {
        #[arg(long)]
        addon_root: PathBuf,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value_t = false)]
        no_deduplicate: bool,
        #[arg(long, default_value_t = 8)]
        pack_workers: usize,
    },
    /// Serve an already-compiled bundle on a loopback-only HTTP endpoint.
    Serve {
        #[arg(long)]
        bundle: PathBuf,
        #[arg(long, default_value = "127.0.0.1")]
        host: IpAddr,
        #[arg(long, default_value_t = 5050)]
        port: u16,
        #[arg(long, value_enum, default_value_t = CliLookupMode::Sorted)]
        lookup_mode: CliLookupMode,
        #[arg(long, value_enum, default_value_t = CliAssetMode::Pack)]
        asset_mode: CliAssetMode,
        #[arg(long)]
        legacy_root: Option<PathBuf>,
        #[arg(long, default_value_t = 4096)]
        response_cache_entries: u64,
        #[arg(long, default_value_t = false)]
        skip_index_checksum: bool,
    },
    /// Run real-data component benchmarks across native and retained-SQLite designs.
    Benchmark {
        #[arg(long)]
        bundle: PathBuf,
        #[arg(long)]
        addon_root: PathBuf,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value_t = 20_000)]
        iterations: usize,
        #[arg(long, default_value_t = 2_000)]
        audio_iterations: usize,
    },
    /// Export the deterministic mixed real-data query corpus used by HTTP benchmarks.
    ExportCorpus {
        #[arg(long)]
        bundle: PathBuf,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value_t = 2048)]
        count: usize,
    },
    /// Fully validate manifest/index structure and optionally hash the multi-GiB pack.
    Verify {
        #[arg(long)]
        bundle: PathBuf,
        #[arg(long, default_value_t = false)]
        full_pack_hash: bool,
    },
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliLookupMode {
    Sorted,
    Mph,
    Preload,
}

impl From<CliLookupMode> for LookupMode {
    fn from(value: CliLookupMode) -> Self {
        match value {
            CliLookupMode::Sorted => Self::Sorted,
            CliLookupMode::Mph => Self::Mph,
            CliLookupMode::Preload => Self::Preload,
        }
    }
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliAssetMode {
    Pack,
    Files,
}

impl From<CliAssetMode> for AssetMode {
    fn from(value: CliAssetMode) -> Self {
        match value {
            CliAssetMode::Pack => Self::Pack,
            CliAssetMode::Files => Self::Files,
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Compile {
            addon_root,
            output,
            no_deduplicate,
            pack_workers,
        } => {
            let manifest = compiler::compile(&CompileOptions {
                addon_root,
                output,
                deduplicate: !no_deduplicate,
                pack_workers,
            })?;
            println!("{}", serde_json::to_string_pretty(&manifest)?);
        }
        Command::Serve {
            bundle,
            host,
            port,
            lookup_mode,
            asset_mode,
            legacy_root,
            response_cache_entries,
            skip_index_checksum,
        } => {
            server::serve(ServeOptions {
                bundle_root: bundle,
                host,
                port,
                lookup_mode: lookup_mode.into(),
                asset_mode: asset_mode.into(),
                legacy_root,
                response_cache_entries,
                verify_index: !skip_index_checksum,
            })
            .await?;
        }
        Command::Benchmark {
            bundle,
            addon_root,
            output,
            iterations,
            audio_iterations,
        } => benchmark::run(&bundle, &addon_root, &output, iterations, audio_iterations)?,
        Command::ExportCorpus {
            bundle,
            output,
            count,
        } => benchmark::export_corpus(&bundle, &output, count)?,
        Command::Verify {
            bundle,
            full_pack_hash,
        } => benchmark::verify(&bundle, full_pack_hash)?,
    }
    Ok(())
}
