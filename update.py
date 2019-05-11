#!/usr/bin/python2.7
import os,uuid,json,time,argparse,contextlib
import requests
import sqlite3

from operator import itemgetter

def write_file_atomic(filename, content):
    tmp_filename = os.path.join(os.path.dirname(os.path.abspath(filename)), str(uuid.uuid4()) + ".tmp")
    with open(tmp_filename, "w") as f:
        f.write(content)
    os.rename(tmp_filename, filename)

def should_download_new_file(filename, ttl_minutes = 60, self_filename = __file__):
    if not os.path.exists(filename) or ttl_minutes is None: return True
    mtime = os.path.getmtime(filename)
    if os.path.getmtime(self_filename) > mtime: return True
    return (time.time() - mtime) / 60 >= ttl_minutes

def load_data(name, url = None, ttl_minutes = 15):
    filename = name + ".json"
    if url is not None and should_download_new_file(filename, ttl_minutes):
        r = requests.get(url)
        write_file_atomic(filename, r.content)
        return json.loads(r.content)
    #else
    return json.load(open(filename))

def calc_earnings_24h(coin, transactions):
    with contextlib.closing(sqlite3.connect("mph.db")) as conn:
        c = conn.cursor()
        c.execute("create table if not exists transactions(id int primary key,coin varchar(32) not null,t timestamp not null,amount float not null)")
        c.execute("create index if not exists coin_idx on transactions(coin)")
        c.execute("create index if not exists t_idx on transactions(t)")

        for txn in transactions:
            if txn["type"] != "Credit" or txn["blockhash"] is None: continue
            c.execute("insert or replace into transactions(id,coin,t,amount) values(?,?,?,?)", (txn["id"], coin,txn["timestamp"], txn["amount"]))

        conn.commit()

        row = c.execute("select sum(amount) from transactions where coin=? and t > datetime(current_timestamp, '-24 hours')", (coin,)).fetchone()
        return row[0] if row is not None else 0.0

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output", type = str, default = "mph.json")
    args = parser.parse_args()

    try:
        config = load_data("config")
    except:
        raise Exception("Error reading config.json.")

    eur = load_data("eur", "http://data.fixer.io/api/latest?access_key=%s" % config["fixer_api_access_key"],60)
    if not eur["success"] or "rates" not in eur:
        raise Exception("Error in Fixer.io. check eur.json.")

    btcjpy = eur["rates"]["JPY"] / eur["rates"]["BTC"]
    usdjpy = eur["rates"]["JPY"] / eur["rates"]["USD"]
    #print "btcjpy=%d, usdjpy=%.2f" % (btcjpy, usdjpy)

    mph_profit_stats = load_data("mph-profit-stats", "https://miningpoolhub.com/index.php?page=api&action=getminingandprofitsstatistics")
    if not mph_profit_stats["success"] or "return" not in mph_profit_stats:
        raise Exception("Error in MiningPoolHub. check mph_profit_stats.json.")

    profit_stats = {}
    for coin in mph_profit_stats["return"]:
        profit_stats[coin["coin_name"]] = coin

    coins = {}
    for equipment in load_data("equipments"):
        for coin_name,performance in equipment["performance"].iteritems():
            if coin_name not in profit_stats:
                raise Exception("Coin %s is not in MiningPoolHub. check equipments.json." % coin_name)
            #else
            hashrate, wattage = performance
            algo = profit_stats[coin_name]["algo"]
            hashrate_scale = 1000.0 if (algo == "Ethash" or algo == "X16r") else 1.0 # It's MH/s in Ethash/X16r
            daily_profit_yen_per_hashrate = profit_stats[coin_name]["profit"] * btcjpy * hashrate_scale / 1000000000.0
            if coin_name not in coins: coins[coin_name] = {"name":coin_name,"algo":algo,"price_yen":profit_stats[coin_name]["highest_buy_price"] * btcjpy,"daily_profit_yen_per_hashrate":daily_profit_yen_per_hashrate,"equipments":[]}
            if algo == "Ethash": hashrate *= hashrate_scale
            daily_profit_yen = daily_profit_yen_per_hashrate * hashrate
            yen_per_kwh = daily_profit_yen / (wattage * 24 / 1000.0)
            coins[coin_name]["equipments"].append({
                "name":equipment["name"],
                "daily_profit_yen":daily_profit_yen,
                "yen_per_kwh":yen_per_kwh
            })

    mph_api_key = config["mph_api_key"]
    mph_user_id = config["mph_user_id"]

    workers = {}
    total_daily_profit_yen = 0
    total_balance_yen = 0
    total_earnings_24h_yen = 0

    for coin_name,coin in coins.iteritems():
        # pick the best power efficiency of the coin
        best_yen_per_kwh = 0
        if "equipments" in coin:
            equipments = coin["equipments"]
            equipments.sort(key=itemgetter("yen_per_kwh"),reverse=True)
            coin["best_yen_per_kwh"] = equipments[0]["yen_per_kwh"] if len(equipments) > 0 else 0

        balance = load_data("balance-%s" % coin_name, "https://%s.miningpoolhub.com/index.php?page=api&action=getuserbalance&api_key=%s&id=%d" % (coin_name, mph_api_key, mph_user_id))
        if "getuserbalance" in balance:
            confirmed = balance["getuserbalance"]["data"]["confirmed"] * coin["price_yen"]
            unconfirmed = balance["getuserbalance"]["data"]["unconfirmed"] * coin["price_yen"]
            coin["confirmed_balance_yen"] = confirmed
            coin["unconfirmed_balance_yen"] = unconfirmed
            total_balance_yen += confirmed
        hashrate_json = load_data("hashrate-%s" % coin_name, "https://%s.miningpoolhub.com/index.php?page=api&action=getuserhashrate&api_key=%s&id=%d" % (coin_name, mph_api_key, mph_user_id))
        if "getuserhashrate" in hashrate_json:
            hashrate = hashrate_json["getuserhashrate"]["data"]
            if coin["algo"] == "Equihash-BTG": hashrate *= 1000
            coin["hashrate"] = hashrate
            daily_profit_yen = coin["daily_profit_yen_per_hashrate"] * hashrate
            coin["daily_profit_yen"] = daily_profit_yen
            total_daily_profit_yen += daily_profit_yen

        _workers = load_data("worker-%s" % coin_name, "https://%s.miningpoolhub.com/index.php?page=api&action=getuserworkers&api_key=%s&id=%d" % (coin_name, mph_api_key, mph_user_id))
        if "getuserworkers" in _workers:
            num_active_workers = 0
            for worker in _workers["getuserworkers"]["data"]:
                worker_name = worker["username"]
                hashrate = worker["hashrate"]
                if hashrate < 0.00000001: continue
                if coin["algo"] == "Equihash-BTG": hashrate *= 1000
                if worker_name not in workers: workers[worker_name] = {"name":worker_name,"coins":[]}
                workers[worker_name]["coins"].append({
                    "name":coin_name,
                    "hashrate":hashrate,
                    "daily_profit_yen":coin["daily_profit_yen_per_hashrate"] * hashrate
                })
                num_active_workers += 1
            coin["num_active_workers"] = num_active_workers

        transactions = load_data("transactions-%s" % coin_name, "https://%s.miningpoolhub.com/index.php?page=api&action=getusertransactions&api_key=%s&id=%d" % (coin_name, mph_api_key, mph_user_id))
        coin["earnings_24h"] = calc_earnings_24h(coin_name, transactions["getusertransactions"]["data"]["transactions"])
        coin["earnings_24h_yen"] = int(coin["earnings_24h"] * coin["price_yen"]) if coin["earnings_24h"] is not None else 0
        total_earnings_24h_yen += coin["earnings_24h_yen"]

    coins = coins.values()
    coins.sort(key=itemgetter("best_yen_per_kwh"), reverse=True)

    workers = workers.values()
    for worker in workers:
        daily_profit_yen = 0
        for coin in worker["coins"]:
            if "daily_profit_yen" in coin: daily_profit_yen += coin["daily_profit_yen"]
        worker["daily_profit_yen"] = daily_profit_yen
        worker["coins"].sort(key=itemgetter("daily_profit_yen"),reverse=True)
    workers.sort(key=itemgetter("daily_profit_yen"), reverse=True)

    write_file_atomic(args.output, json.dumps({"btcjpy":btcjpy, "usdjpy":usdjpy, "coins":coins, "workers":workers, "daily_profit_yen":total_daily_profit_yen, "balance_yen":total_balance_yen,"earnings_24h_yen":total_earnings_24h_yen}))
