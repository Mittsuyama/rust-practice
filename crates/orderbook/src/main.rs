use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde::{Deserialize, de::DeserializeOwned};
use std::path::{Path, PathBuf};

#[derive(Debug, Subcommand)]
enum Cmd {
    Stats {
        #[arg(long)]
        orders: PathBuf,
        #[arg(long)]
        trades: PathBuf,
    },
}

#[derive(Debug, Parser)]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
enum OrderType {
    #[serde(rename = "LIMIT")]
    Limit,
    #[serde(rename = "MARKET")]
    Market,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
enum Side {
    #[serde(rename = "B")]
    Buy,
    #[serde(rename = "S")]
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
enum Action {
    #[serde(rename = "A")]
    Add,
    #[serde(rename = "C")]
    Cancel,
}

#[derive(Debug, serde::Deserialize)]
struct OrderRecord {
    trade_date: String,
    security_id: String,
    order_time: String,
    order_no: u32,
    side: Side,
    order_type: OrderType,
    action: Action,
    price: String,
    qty: u32,
}

#[derive(Debug, serde::Deserialize)]
struct TradeRecord {
    trade_date: String,
    security_id: String,
    trade_time: String,
    trade_no: u32,
    buy_order_no: u32,
    sell_order_no: u32,
    price: String,
    qty: u32,
    aggressor_side: Side,
}

fn get_records_from_path<T: DeserializeOwned>(path: &Path) -> Result<Vec<T>> {
    let mut reader =
        csv::Reader::from_path(path).with_context(|| format!("Open {} failed.", path.display()))?;

    let records = reader
        .deserialize::<T>()
        .collect::<Result<Vec<T>, csv::Error>>()?;

    Ok(records)
}

fn stats(orders: &Path, trades: &Path) -> Result<()> {
    let orders = get_records_from_path::<OrderRecord>(orders)?;
    let trades = get_records_from_path::<TradeRecord>(trades)?;
    println!("orders.len: {}", orders.len());
    dbg!(&orders[0]);
    dbg!(&trades[0]);
    Ok(())
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Stats { orders, trades } => stats(&orders, &trades),
    }
}
