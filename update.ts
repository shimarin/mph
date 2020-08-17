#!/usr/bin/env -S deno run --unstable --allow-read --allow-write --allow-net
import { existsSync } from "https://deno.land/std/fs/mod.ts";
import { difference } from 'https://deno.land/std/datetime/mod.ts';
import { DB } from "https://deno.land/x/sqlite/mod.ts";

function load_json_file(filename:string):any
{
    return JSON.parse(Deno.readTextFileSync(filename));
}

function with_db(func:(db:DB)=>void) {
    const db = new DB("mph.db");
    try {
        func(db);
    }
    finally {
        db.close();
    }
}

function should_download_new_file(filename:string, ttl_minutes:number):boolean
{
    if (!existsSync(filename)) return true;
    return difference(Deno.statSync(filename).mtime!, new Date(), {units:["minutes"]}).minutes! > ttl_minutes;
}

function write_file_atomic(filename:string, data:any):void
{
    Deno.writeTextFileSync(filename + ".tmp", JSON.stringify(data));
    Deno.renameSync(filename + ".tmp", filename);
}

async function load_data(name:string, url?:string, ttl_minutes = 15):Promise<any>
{
    const filename = name + ".json";
    if (url && should_download_new_file(filename, ttl_minutes)) {
        console.log(`Downloading ${url} ...`);
        return fetch(url).then(res => res.json()).then(json => {
            write_file_atomic(filename, json);
            return json;
        }).catch(reason=>load_json_file(filename));
    }
    //else
    return load_json_file(filename);
}

const config = load_json_file("config.json");

with_db( db => {
    db.query("create table if not exists transactions(id int primary key,coin varchar(32) not null,t timestamp not null,amount float not null)");
    db.query("create index if not exists coin_idx on transactions(coin)");
    db.query("create index if not exists t_idx on transactions(t)");
});

//console.log(config.mph_api_key);
//console.log(config.mph_user_id);

const equipments:{name:string,performance:{[key: string]:number[]}}[] = load_json_file("equipments.json");

const [eur, mph_profit_stats]:[{rates:{JPY:number,BTC:number,USD:number}},{return:{coin_name:string,highest_buy_price:number,algo:string,profit:number}[]}] = await Promise.all([
    load_data("eur", `http://data.fixer.io/api/latest?access_key=${config.fixer_api_access_key}`, 60),
    load_data("mph-profit-stats", "https://miningpoolhub.com/index.php?page=api&action=getminingandprofitsstatistics")
]);

const profit_stats = new Map(mph_profit_stats.return.map (coin => [coin.coin_name,coin]));

const btcjpy = eur.rates.JPY / eur.rates.BTC;
const usdjpy = eur.rates.JPY / eur.rates.USD;

const coins = new Map<string,{name:string,price_yen:number,best_yen_per_kwh:number,algo:string,daily_profit_yen_per_hashrate:number,equipments:{name:string,daily_profit_yen:number,yen_per_kwh:number}[]}>();

coins.set("bitcoin", {
    name:"bitcoin",
    price_yen:profit_stats.get("bitcoin")!.highest_buy_price * btcjpy,
    best_yen_per_kwh:100000,
    algo:"sha256",
    daily_profit_yen_per_hashrate:0,
    equipments:[]
});

equipments.forEach(equipment => {
    for (const coin_name in equipment.performance) {
        let [hashrate,wattage] = equipment.performance[coin_name];
        const algo = profit_stats.get(coin_name)!.algo;
        const hashrate_scale = (algo == "Ethash" || algo == "X16r")? 1000.0 : 1.0;
        const daily_profit_yen_per_hashrate = profit_stats.get(coin_name)!.profit * btcjpy * hashrate_scale / 1000000000.0;
        if (!coins.has(coin_name)) coins.set(coin_name, {name:coin_name,algo:algo,price_yen:profit_stats.get(coin_name)!.highest_buy_price * btcjpy,best_yen_per_kwh:0, daily_profit_yen_per_hashrate:daily_profit_yen_per_hashrate,equipments:[]});
        if (algo == "Ethash") hashrate *= hashrate_scale;
        const daily_profit_yen = daily_profit_yen_per_hashrate * hashrate;
        coins.get(coin_name)?.equipments.push({
            name:equipment.name,
            daily_profit_yen:daily_profit_yen,
            yen_per_kwh:daily_profit_yen / (wattage * 24 / 1000.0)
        });
    }
});

Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify({btcjpy:btcjpy,usdjpy:usdjpy,coins:Object.fromEntries(coins),workers:[],daily_profit_yen:0,balance_yen:0,earnings_24h_yen:0})));
