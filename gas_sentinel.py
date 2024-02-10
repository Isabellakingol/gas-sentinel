mas3
gas-sentinel
Multi-chain gas watchdog that polls EVM RPCs, persists a queue of signed transactions, and broadcasts when gas is under threshold. Includes jitter, backoff, and resume-safe state.
#!/usr/bin/env python3
# gas-sentinel
# Purpose: queue and broadcast signed txs when basefee <= threshold on any configured chain.
# Usage:
#   pip install web3 python-dotenv pyyaml
#   export ENV=.env && python3 gas_sentinel.py
# Config (.env):
#   CHAINS=ethereum,bsc,polygon
#   RPC_ETHEREUM=https://mainnet.infura.io/v3/YOUR_KEY
#   RPC_BSC=https://bsc-dataseed.binance.org/
#   RPC_POLYGON=https://polygon-rpc.com/
#   MAX_FEE_GWEI=18
#   POLL_SEC=15
#   QUEUE_FILE=queue.yaml
#   STATE_FILE=state.json
# Queue format (queue.yaml):
#   - chain: ethereum
#     rawTx: 0x02f86b...
#     label: treasury-topup
#     minBaseFeeGwei: 14
#     attempts: 0
import os, time, json, random, yaml, hashlib
from dataclasses import dataclass, asdict
from typing import Dict, List
from web3 import Web3
from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV", ".env"))

def gwei(x): return int(float(x) * 1e9)

@dataclass
class ChainCfg:
    name: str
    rpc: str

@dataclass
class TxItem:
    chain: str
    rawTx: str
    label: str
    minBaseFeeGwei: int
    attempts: int = 0

class GasSentinel:
    def __init__(self):
        self.chains: Dict[str, ChainCfg] = {}
        self.web3s: Dict[str, Web3] = {}
        self.queue_file = os.getenv("QUEUE_FILE", "queue.yaml")
        self.state_file = os.getenv("STATE_FILE", "state.json")
        self.max_fee_gwei = int(os.getenv("MAX_FEE_GWEI", "20"))
        self.poll_sec = int(os.getenv("POLL_SEC", "15"))
        self._load_chains()
        self.queue: List[TxItem] = self._load_queue()
        self.state = self._load_state()

    def _load_chains(self):
        for n in (os.getenv("CHAINS","").split(",")):
            name = n.strip()
            if not name: continue
            rpc = os.getenv(f"RPC_{name.upper()}", "")
            if not rpc:
                print(f"[WARN] RPC for {name} not set, skipping")
                continue
            self.chains[name] = ChainCfg(name, rpc)
            self.web3s[name] = Web3(Web3.HTTPProvider(rpc))
        if not self.web3s:
            raise SystemExit("No chains configured")

    def _load_queue(self) -> List[TxItem]:
        if not os.path.exists(self.queue_file): return []
        with open(self.queue_file) as f:
            data = yaml.safe_load(f) or []
            out = []
            for row in data:
                out.append(TxItem(
                    chain=row["chain"],
                    rawTx=row["rawTx"],
                    label=row.get("label",""),
                    minBaseFeeGwei=int(row.get("minBaseFeeGwei", self.max_fee_gwei)),
                    attempts=int(row.get("attempts",0))
                ))
            return out

    def _save_queue(self):
        with open(self.queue_file, "w") as f:
            yaml.safe_dump([asdict(x) for x in self.queue], f, sort_keys=False)

    def _load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                return json.load(f)
        return {"broadcasted": {}}

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def basefee_gwei(self, chain: str) -> int:
        w3 = self.web3s[chain]
        try:
            bf = w3.eth.get_block("latest").baseFeePerGas
        except Exception:
            bf = w3.eth.gas_price
        return int(bf // 1_000_000_000)

    def broadcast(self, chain: str, raw: str) -> str:
        w3 = self.web3s[chain]
        tx_hash = w3.eth.send_raw_transaction(bytes.fromhex(raw[2:]))
        return tx_hash.hex()

    def key(self, item: TxItem) -> str:
        return hashlib.sha1(f"{item.chain}:{item.rawTx}".encode()).hexdigest()

    def run(self):
        print(f"[INFO] chains: {list(self.web3s.keys())}, queue={len(self.queue)}")
        while True:
            for item in list(self.queue):
                try:
                    bf = self.basefee_gwei(item.chain)
                    should = bf <= item.minBaseFeeGwei and bf <= self.max_fee_gwei
                    print(f"[DBG] {item.chain} basefee={bf} gwei label={item.label} min={item.minBaseFeeGwei} -> {'OK' if should else 'WAIT'}")
                    if should:
                        h = self.broadcast(item.chain, item.rawTx)
                        self.state["broadcasted"][self.key(item)] = {"hash": h, "ts": int(time.time())}
                        print(f"[OK] broadcast {item.label} on {item.chain}: {h}")
                        self.queue.remove(item); self._save_queue(); self._save_state()
                    else:
                        item.attempts += 1
                        if item.attempts % 20 == 0: self._save_queue()
                except Exception as e:
                    print(f"[ERR] {item.label} on {item.chain}: {e}")
            time.sleep(self.poll_sec + random.randint(0,5))

if __name__ == "__main__":
    GasSentinel().run()
