"""
Multi-exchange coin discovery - fetches ALL available perpetual contracts
Supports: BingX, BloFin, Hyperliquid
"""
import asyncio
import aiohttp
from dataclasses import dataclass
from typing import List, Dict, Optional
from datetime import datetime
import time


@dataclass
class PerpContract:
    """Represents a perpetual contract available for trading"""
    symbol: str           # e.g., "WIF-USDT"
    base_coin: str        # e.g., "WIF"
    quote_coin: str       # e.g., "USDT"
    exchange: str         # "bingx" or "blofin"
    list_time: int        # Unix timestamp when listed
    max_leverage: int
    min_size: float
    api_enabled: bool
    
    @property
    def age_hours(self) -> float:
        """Hours since listing"""
        return (time.time() * 1000 - self.list_time) / (1000 * 60 * 60)
    
    @property
    def age_days(self) -> float:
        return self.age_hours / 24
    
    @property
    def is_new(self) -> bool:
        """Listed within last 7 days"""
        return self.age_days < 7
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "base_coin": self.base_coin,
            "quote_coin": self.quote_coin,
            "exchange": self.exchange,
            "list_time": self.list_time,
            "list_date": datetime.fromtimestamp(self.list_time / 1000).strftime("%Y-%m-%d") if self.list_time > 0 else "Unknown",
            "age_days": round(self.age_days, 1),
            "max_leverage": self.max_leverage,
            "is_new": self.is_new,
            "api_enabled": self.api_enabled
        }


class ExchangeDiscovery:
    """Discovers all available perpetual contracts across exchanges"""
    
    def __init__(self):
        self.contracts: Dict[str, PerpContract] = {}  # key: "exchange:symbol"
        self.last_refresh: float = 0
        
    async def fetch_bingx(self) -> List[PerpContract]:
        """Fetch all perpetual contracts from BingX"""
        contracts = []
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("data", []):
                            if c.get("apiStateOpen") == "true":
                                contracts.append(PerpContract(
                                    symbol=c["symbol"],
                                    base_coin=c.get("asset", c["symbol"].split("-")[0]),
                                    quote_coin=c.get("currency", "USDT"),
                                    exchange="bingx",
                                    list_time=int(c.get("launchTime", 0)),
                                    max_leverage=100,  # BingX doesn't expose this easily
                                    min_size=float(c.get("tradeMinQuantity", 0)),
                                    api_enabled=c.get("apiStateOpen") == "true"
                                ))
        except Exception as e:
            print(f"Error fetching BingX contracts: {e}")
            
        return contracts
    
    async def fetch_blofin(self) -> List[PerpContract]:
        """Fetch all perpetual contracts from BloFin"""
        contracts = []
        url = "https://openapi.blofin.com/api/v1/market/instruments?instType=SWAP"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for c in data.get("data", []):
                            if c.get("state") == "live":
                                contracts.append(PerpContract(
                                    symbol=c["instId"],
                                    base_coin=c.get("baseCurrency", c["instId"].split("-")[0]),
                                    quote_coin=c.get("quoteCurrency", "USDT"),
                                    exchange="blofin",
                                    list_time=int(c.get("listTime", 0)),
                                    max_leverage=int(c.get("maxLeverage", 0)),
                                    min_size=float(c.get("minSize", 0)),
                                    api_enabled=True
                                ))
        except Exception as e:
            print(f"Error fetching BloFin contracts: {e}")
            
        return contracts
    
    async def fetch_hyperliquid(self) -> List[PerpContract]:
        """Fetch all perpetual contracts from Hyperliquid"""
        contracts = []
        url = "https://api.hyperliquid.xyz/info"
        
        try:
            async with aiohttp.ClientSession() as session:
                # Get meta info (market list)
                async with session.post(url, json={"type": "meta"}, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        universe = data.get("universe", [])
                        
                        # Give Hyperliquid a recent list_time since they don't expose it
                        # Use current time minus index to create ordering
                        base_time = int(time.time() * 1000) - (3 * 24 * 60 * 60 * 1000)  # 3 days ago (shows in "New")
                        
                        for i, m in enumerate(universe):
                            # Hyperliquid uses coin name without quote (e.g., "WIF" not "WIF-USDT")
                            # All are USD settled
                            contracts.append(PerpContract(
                                symbol=m["name"],  # e.g., "WIF"
                                base_coin=m["name"],
                                quote_coin="USD",
                                exchange="hyperliquid",
                                list_time=base_time - (i * 1000),  # Stagger times so they have ordering
                                max_leverage=int(m.get("maxLeverage", 50)),
                                min_size=float(m.get("szDecimals", 0)),
                                api_enabled=True
                            ))
        except Exception as e:
            print(f"Error fetching Hyperliquid contracts: {e}")
            
        return contracts
    
    async def refresh(self) -> int:
        """Refresh all contracts from all exchanges"""
        print("Refreshing contract lists...")
        
        # Fetch in parallel
        bingx_task = self.fetch_bingx()
        blofin_task = self.fetch_blofin()
        hyperliquid_task = self.fetch_hyperliquid()
        
        bingx_contracts, blofin_contracts, hl_contracts = await asyncio.gather(
            bingx_task, blofin_task, hyperliquid_task
        )
        
        # Merge into contracts dict
        self.contracts.clear()
        
        for c in bingx_contracts:
            key = f"bingx:{c.symbol}"
            self.contracts[key] = c
            
        for c in blofin_contracts:
            key = f"blofin:{c.symbol}"
            self.contracts[key] = c
            
        for c in hl_contracts:
            key = f"hyperliquid:{c.symbol}"
            self.contracts[key] = c
            
        self.last_refresh = time.time()
        
        print(f"  BingX: {len(bingx_contracts)} contracts")
        print(f"  BloFin: {len(blofin_contracts)} contracts")
        print(f"  Hyperliquid: {len(hl_contracts)} contracts")
        print(f"  Total: {len(self.contracts)} contracts")
        
        return len(self.contracts)
    
    def get_all(self, sort_by: str = "list_time", exchange: str = None) -> List[dict]:
        """Get all contracts, optionally filtered and sorted"""
        contracts = list(self.contracts.values())
        
        # Filter by exchange
        if exchange:
            contracts = [c for c in contracts if c.exchange == exchange]
            
        # Sort
        if sort_by == "list_time":
            contracts.sort(key=lambda x: x.list_time, reverse=True)  # Newest first
        elif sort_by == "symbol":
            contracts.sort(key=lambda x: x.symbol)
        elif sort_by == "leverage":
            contracts.sort(key=lambda x: x.max_leverage, reverse=True)
            
        return [c.to_dict() for c in contracts]
    
    def get_new_listings(self, days: int = 7) -> List[dict]:
        """Get contracts listed within the last N days"""
        cutoff = time.time() - (days * 24 * 60 * 60)
        contracts = [c for c in self.contracts.values() if c.list_time / 1000 > cutoff]
        contracts.sort(key=lambda x: x.list_time, reverse=True)
        return [c.to_dict() for c in contracts]
    
    def search(self, query: str) -> List[dict]:
        """Search contracts by symbol or base coin"""
        query = query.upper()
        contracts = [c for c in self.contracts.values() 
                    if query in c.symbol.upper() or query in c.base_coin.upper()]
        contracts.sort(key=lambda x: x.list_time, reverse=True)
        return [c.to_dict() for c in contracts]
    
    def get_contract(self, exchange: str, symbol: str) -> Optional[PerpContract]:
        """Get a specific contract"""
        key = f"{exchange}:{symbol}"
        return self.contracts.get(key)


# Test
async def main():
    discovery = ExchangeDiscovery()
    await discovery.refresh()
    
    print("\n--- Newest Listings (last 7 days) ---")
    new = discovery.get_new_listings(7)
    for c in new[:20]:
        print(f"  {c['exchange']:8} | {c['symbol']:20} | Listed: {c['list_date']} ({c['age_days']}d ago)")
    
    print(f"\n--- Search: 'TRUMP' ---")
    results = discovery.search("TRUMP")
    for c in results[:5]:
        print(f"  {c['exchange']:8} | {c['symbol']}")


if __name__ == "__main__":
    asyncio.run(main())
