"""
Trading Signal Engine - Generates buy/sell signals from order flow data

Signals are based on:
1. Order Book Imbalance (OBI) - bid vs ask volume ratio
2. Weighted Book Pressure - orders near mid-price weighted higher
3. Large Order Detection - whale walls as support/resistance
4. Spread Analysis - liquidity indicator
5. Trade Flow Delta - cumulative buy vs sell volume
6. Momentum - rate of change in imbalance
7. Liquidity Zones - far-out order clusters for reversal plays
"""
import time
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from collections import deque
from enum import Enum


class Signal(Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


@dataclass
class LiquidityZone:
    """A cluster of orders at a price level"""
    price: float
    total_volume_usd: float
    side: str  # "bid" or "ask"
    distance_pct: float  # Distance from current price
    order_count: int
    is_major: bool  # True if this is a significant zone
    
    def to_dict(self) -> dict:
        return {
            "price": round(self.price, 8),
            "volume_usd": round(self.total_volume_usd, 0),
            "side": self.side,
            "distance_pct": round(self.distance_pct, 2),
            "order_count": self.order_count,
            "is_major": self.is_major,
            "type": "support" if self.side == "bid" else "resistance"
        }


@dataclass  
class TradeSuggestion:
    """Suggested entry based on analysis"""
    action: str  # "LONG", "SHORT", "WAIT"
    mode: str  # "scalp" or "reversal"
    entry_price: float
    target_price: float
    stop_price: float
    confidence: float
    reason: str
    
    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "mode": self.mode,
            "entry_price": round(self.entry_price, 8),
            "target_price": round(self.target_price, 8),
            "stop_price": round(self.stop_price, 8),
            "confidence": round(self.confidence, 1),
            "reason": self.reason
        }


@dataclass
class OrderBookLevel:
    price: float
    quantity: float
    
    @property
    def value_usd(self) -> float:
        return self.price * self.quantity


@dataclass
class SignalResult:
    """Result of signal analysis"""
    signal: Signal
    confidence: float  # 0-100
    score: float  # -100 to +100 (negative = sell, positive = buy)
    
    # Component scores (all -100 to +100)
    imbalance_score: float = 0
    weighted_pressure_score: float = 0
    wall_score: float = 0
    spread_score: float = 0
    flow_score: float = 0
    momentum_score: float = 0
    
    # Raw metrics
    bid_volume: float = 0
    ask_volume: float = 0
    imbalance_ratio: float = 1.0
    spread_bps: float = 0
    largest_bid_usd: float = 0
    largest_ask_usd: float = 0
    mid_price: float = 0
    
    # Liquidity zones (far-out clusters)
    support_zones: List[LiquidityZone] = field(default_factory=list)
    resistance_zones: List[LiquidityZone] = field(default_factory=list)
    
    # Trade suggestions
    scalp_suggestion: Optional[TradeSuggestion] = None
    reversal_suggestion: Optional[TradeSuggestion] = None
    
    reasons: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "signal": self.signal.value,
            "confidence": round(self.confidence, 1),
            "score": round(self.score, 1),
            "components": {
                "imbalance": round(self.imbalance_score, 1),
                "weighted_pressure": round(self.weighted_pressure_score, 1),
                "walls": round(self.wall_score, 1),
                "spread": round(self.spread_score, 1),
                "flow": round(self.flow_score, 1),
                "momentum": round(self.momentum_score, 1),
            },
            "metrics": {
                "bid_volume": round(self.bid_volume, 0),
                "ask_volume": round(self.ask_volume, 0),
                "imbalance_ratio": round(self.imbalance_ratio, 3),
                "spread_bps": round(self.spread_bps, 2),
                "largest_bid_usd": round(self.largest_bid_usd, 0),
                "largest_ask_usd": round(self.largest_ask_usd, 0),
                "mid_price": self.mid_price,
            },
            "liquidity_zones": {
                "support": [z.to_dict() for z in self.support_zones[:5]],
                "resistance": [z.to_dict() for z in self.resistance_zones[:5]],
            },
            "suggestions": {
                "scalp": self.scalp_suggestion.to_dict() if self.scalp_suggestion else None,
                "reversal": self.reversal_suggestion.to_dict() if self.reversal_suggestion else None,
            },
            "reasons": self.reasons
        }


class SignalEngine:
    """
    Analyzes order book and trade flow to generate trading signals
    """
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        
        # Historical data for momentum/trend analysis
        self.imbalance_history: deque = deque(maxlen=60)  # Last 60 updates (~30 sec)
        self.trade_deltas: deque = deque(maxlen=100)  # Recent trade deltas
        self.cumulative_delta: float = 0  # Running buy - sell volume
        
        # Configurable weights for final score
        self.weights = {
            "imbalance": 0.25,
            "weighted_pressure": 0.20,
            "walls": 0.15,
            "spread": 0.10,
            "flow": 0.20,
            "momentum": 0.10,
        }
        
    def analyze(
        self, 
        bids: List[OrderBookLevel], 
        asks: List[OrderBookLevel],
        recent_trades: List[Tuple[float, str]] = None  # [(value_usd, "buy"/"sell"), ...]
    ) -> SignalResult:
        """
        Analyze order book and generate signal
        
        Args:
            bids: List of bid levels (price, qty)
            asks: List of ask levels (price, qty)
            recent_trades: Optional list of recent trades for flow analysis
            
        Returns:
            SignalResult with signal, confidence, and breakdown
        """
        result = SignalResult(signal=Signal.NEUTRAL, confidence=0, score=0)
        
        if not bids or not asks:
            result.reasons.append("Insufficient data")
            return result
            
        mid_price = (bids[0].price + asks[0].price) / 2
        
        # 1. Basic Order Book Imbalance
        result.imbalance_score, result.bid_volume, result.ask_volume, result.imbalance_ratio = \
            self._calc_imbalance(bids, asks)
        
        # 2. Weighted Book Pressure (orders near mid weighted higher)
        result.weighted_pressure_score = self._calc_weighted_pressure(bids, asks, mid_price)
        
        # 3. Wall Detection
        result.wall_score, result.largest_bid_usd, result.largest_ask_usd = \
            self._calc_wall_score(bids, asks, result.bid_volume, result.ask_volume)
        
        # 4. Spread Analysis
        result.spread_score, result.spread_bps = self._calc_spread_score(bids, asks)
        
        # 5. Trade Flow Delta
        if recent_trades:
            result.flow_score = self._calc_flow_score(recent_trades)
        
        # 6. Momentum (rate of change in imbalance)
        self.imbalance_history.append(result.imbalance_ratio)
        result.momentum_score = self._calc_momentum()
        
        # Calculate final weighted score
        result.score = (
            result.imbalance_score * self.weights["imbalance"] +
            result.weighted_pressure_score * self.weights["weighted_pressure"] +
            result.wall_score * self.weights["walls"] +
            result.spread_score * self.weights["spread"] +
            result.flow_score * self.weights["flow"] +
            result.momentum_score * self.weights["momentum"]
        )
        
        # Determine signal and confidence
        result.signal, result.confidence = self._score_to_signal(result.score)
        
        # Store mid price for suggestions
        result.mid_price = mid_price
        
        # Find liquidity zones (far-out clusters)
        result.support_zones, result.resistance_zones = self._find_liquidity_zones(
            bids, asks, mid_price
        )
        
        # Generate trade suggestions
        result.scalp_suggestion, result.reversal_suggestion = self._generate_suggestions(
            result, bids, asks
        )
        
        # Generate human-readable reasons
        result.reasons = self._generate_reasons(result)
        
        # Add liquidity zone reasons
        if result.support_zones and result.support_zones[0].is_major:
            z = result.support_zones[0]
            result.reasons.append(f"🟢 Major support at ${z.price:.6f} ({z.distance_pct:.1f}% below)")
        if result.resistance_zones and result.resistance_zones[0].is_major:
            z = result.resistance_zones[0]
            result.reasons.append(f"🔴 Major resistance at ${z.price:.6f} ({z.distance_pct:.1f}% above)")
        
        return result
    
    def _calc_imbalance(
        self, 
        bids: List[OrderBookLevel], 
        asks: List[OrderBookLevel],
        depth: int = 20
    ) -> Tuple[float, float, float, float]:
        """
        Calculate order book imbalance score
        
        Returns: (score, bid_volume, ask_volume, ratio)
        """
        bid_volume = sum(b.value_usd for b in bids[:depth])
        ask_volume = sum(a.value_usd for a in asks[:depth])
        
        if ask_volume == 0:
            ratio = 2.0
        elif bid_volume == 0:
            ratio = 0.5
        else:
            ratio = bid_volume / ask_volume
        
        # Convert ratio to -100 to +100 score
        # ratio 1.0 = 0, ratio 2.0 = +50, ratio 0.5 = -50
        if ratio >= 1:
            score = min(100, (ratio - 1) * 50)
        else:
            score = max(-100, (ratio - 1) * 100)
            
        return score, bid_volume, ask_volume, ratio
    
    def _calc_weighted_pressure(
        self, 
        bids: List[OrderBookLevel], 
        asks: List[OrderBookLevel],
        mid_price: float,
        decay_rate: float = 0.1
    ) -> float:
        """
        Calculate pressure with orders near mid-price weighted higher
        Uses exponential decay based on distance from mid
        """
        bid_pressure = 0
        ask_pressure = 0
        
        for bid in bids[:30]:
            distance_pct = (mid_price - bid.price) / mid_price
            weight = math.exp(-decay_rate * distance_pct * 100)  # Decay with distance
            bid_pressure += bid.value_usd * weight
            
        for ask in asks[:30]:
            distance_pct = (ask.price - mid_price) / mid_price
            weight = math.exp(-decay_rate * distance_pct * 100)
            ask_pressure += ask.value_usd * weight
        
        total = bid_pressure + ask_pressure
        if total == 0:
            return 0
            
        # Convert to -100 to +100
        imbalance = (bid_pressure - ask_pressure) / total
        return imbalance * 100
    
    def _calc_wall_score(
        self, 
        bids: List[OrderBookLevel], 
        asks: List[OrderBookLevel],
        total_bid: float,
        total_ask: float
    ) -> Tuple[float, float, float]:
        """
        Detect large orders (walls) and score their impact
        Walls near current price = stronger signal
        """
        largest_bid = max(bids[:20], key=lambda x: x.value_usd) if bids else None
        largest_ask = max(asks[:20], key=lambda x: x.value_usd) if asks else None
        
        largest_bid_usd = largest_bid.value_usd if largest_bid else 0
        largest_ask_usd = largest_ask.value_usd if largest_ask else 0
        
        # Score based on relative size of walls
        # A wall that's 20%+ of visible liquidity is significant
        bid_wall_pct = (largest_bid_usd / total_bid * 100) if total_bid > 0 else 0
        ask_wall_pct = (largest_ask_usd / total_ask * 100) if total_ask > 0 else 0
        
        # Big bid wall = bullish (support), big ask wall = bearish (resistance)
        score = 0
        
        if bid_wall_pct > 15:
            score += min(50, bid_wall_pct)  # Cap at 50
        if ask_wall_pct > 15:
            score -= min(50, ask_wall_pct)
            
        # Also consider absolute size (whale activity)
        if largest_bid_usd > 100000:  # $100k+
            score += 20
        if largest_ask_usd > 100000:
            score -= 20
            
        return max(-100, min(100, score)), largest_bid_usd, largest_ask_usd
    
    def _calc_spread_score(
        self, 
        bids: List[OrderBookLevel], 
        asks: List[OrderBookLevel]
    ) -> Tuple[float, float]:
        """
        Analyze spread - tight spread = confident market, wide = uncertainty
        Returns neutral-biased score (doesn't predict direction, affects confidence)
        """
        if not bids or not asks:
            return 0, 0
            
        spread = asks[0].price - bids[0].price
        mid = (asks[0].price + bids[0].price) / 2
        spread_bps = (spread / mid) * 10000  # Basis points
        
        # Tight spread (< 5 bps) = good liquidity, neutral
        # Wide spread (> 50 bps) = low liquidity, reduce confidence
        # This doesn't predict direction, just affects overall confidence
        if spread_bps < 5:
            score = 10  # Slight positive for good liquidity
        elif spread_bps > 50:
            score = -10  # Slight negative for poor liquidity
        else:
            score = 0
            
        return score, spread_bps
    
    def _calc_flow_score(self, recent_trades: List[Tuple[float, str]]) -> float:
        """
        Calculate score from recent trade flow
        More buy volume = bullish, more sell volume = bearish
        """
        buy_volume = sum(t[0] for t in recent_trades if t[1] == "buy")
        sell_volume = sum(t[0] for t in recent_trades if t[1] == "sell")
        
        total = buy_volume + sell_volume
        if total == 0:
            return 0
            
        delta = buy_volume - sell_volume
        self.cumulative_delta += delta
        
        # Normalize to -100 to +100
        imbalance = delta / total
        return imbalance * 100
    
    def _calc_momentum(self) -> float:
        """
        Calculate momentum based on imbalance trend
        Rising imbalance = bullish momentum
        Falling imbalance = bearish momentum
        """
        if len(self.imbalance_history) < 10:
            return 0
            
        history = list(self.imbalance_history)
        
        # Compare recent average to older average
        recent = sum(history[-10:]) / 10
        older = sum(history[:10]) / 10 if len(history) >= 20 else recent
        
        # Rate of change
        if older == 0:
            return 0
            
        roc = (recent - older) / older
        
        # Convert to score (-100 to +100)
        # 10% increase in imbalance ratio = +30 score
        return max(-100, min(100, roc * 300))
    
    def _score_to_signal(self, score: float) -> Tuple[Signal, float]:
        """Convert numerical score to signal enum and confidence"""
        abs_score = abs(score)
        
        # Confidence is based on how extreme the score is
        # Score of 50+ = high confidence, 20-50 = medium, <20 = low
        confidence = min(100, abs_score * 2)
        
        if score >= 40:
            return Signal.STRONG_BUY, confidence
        elif score >= 20:
            return Signal.BUY, confidence
        elif score <= -40:
            return Signal.STRONG_SELL, confidence
        elif score <= -20:
            return Signal.SELL, confidence
        else:
            return Signal.NEUTRAL, confidence
    
    def _generate_reasons(self, result: SignalResult) -> List[str]:
        """Generate human-readable reasons for the signal"""
        reasons = []
        
        # Imbalance
        if result.imbalance_ratio > 1.3:
            reasons.append(f"📈 Strong bid imbalance ({result.imbalance_ratio:.2f}x more bids)")
        elif result.imbalance_ratio < 0.7:
            reasons.append(f"📉 Strong ask imbalance ({1/result.imbalance_ratio:.2f}x more asks)")
        
        # Walls
        if result.largest_bid_usd > 50000:
            reasons.append(f"🧱 Large bid wall: ${result.largest_bid_usd:,.0f}")
        if result.largest_ask_usd > 50000:
            reasons.append(f"🧱 Large ask wall: ${result.largest_ask_usd:,.0f}")
        
        # Momentum
        if result.momentum_score > 30:
            reasons.append("🚀 Bullish momentum building")
        elif result.momentum_score < -30:
            reasons.append("📉 Bearish momentum building")
        
        # Spread
        if result.spread_bps > 30:
            reasons.append(f"⚠️ Wide spread ({result.spread_bps:.0f} bps) - low liquidity")
        
        # Flow
        if result.flow_score > 40:
            reasons.append("💰 Heavy buy flow detected")
        elif result.flow_score < -40:
            reasons.append("💰 Heavy sell flow detected")
        
        if not reasons:
            reasons.append("📊 No strong signals detected")
            
        return reasons
    
    def _find_liquidity_zones(
        self,
        bids: List[OrderBookLevel],
        asks: List[OrderBookLevel],
        mid_price: float,
        min_distance_pct: float = 0.0,  # Show all zones, even very close to price
        max_distance_pct: float = 50.0,  # Extended range
        cluster_threshold_pct: float = 0.15  # Tighter clustering for better grouping
    ) -> Tuple[List[LiquidityZone], List[LiquidityZone]]:
        """
        Find liquidity clusters far from current price
        
        Groups orders within cluster_threshold_pct into zones,
        only considering orders between min and max distance from mid
        """
        support_zones = []
        resistance_zones = []
        
        if not mid_price or mid_price == 0:
            return support_zones, resistance_zones
        
        # Process bids (support zones)
        bid_clusters = {}
        for bid in bids:
            distance_pct = ((mid_price - bid.price) / mid_price) * 100
            if min_distance_pct <= distance_pct <= max_distance_pct:
                # Round to cluster threshold
                cluster_price = round(bid.price / (mid_price * cluster_threshold_pct / 100)) * (mid_price * cluster_threshold_pct / 100)
                if cluster_price not in bid_clusters:
                    bid_clusters[cluster_price] = {"volume": 0, "count": 0, "prices": []}
                bid_clusters[cluster_price]["volume"] += bid.value_usd
                bid_clusters[cluster_price]["count"] += 1
                bid_clusters[cluster_price]["prices"].append(bid.price)
        
        # Convert bid clusters to zones
        total_bid_volume = sum(c["volume"] for c in bid_clusters.values()) if bid_clusters else 1
        for cluster_price, data in bid_clusters.items():
            avg_price = sum(data["prices"]) / len(data["prices"]) if data["prices"] else cluster_price
            distance = ((mid_price - avg_price) / mid_price) * 100
            is_major = data["volume"] > total_bid_volume * 0.2 or data["volume"] > 100000
            
            support_zones.append(LiquidityZone(
                price=avg_price,
                total_volume_usd=data["volume"],
                side="bid",
                distance_pct=distance,
                order_count=data["count"],
                is_major=is_major
            ))
        
        # Process asks (resistance zones)
        ask_clusters = {}
        for ask in asks:
            distance_pct = ((ask.price - mid_price) / mid_price) * 100
            if min_distance_pct <= distance_pct <= max_distance_pct:
                cluster_price = round(ask.price / (mid_price * cluster_threshold_pct / 100)) * (mid_price * cluster_threshold_pct / 100)
                if cluster_price not in ask_clusters:
                    ask_clusters[cluster_price] = {"volume": 0, "count": 0, "prices": []}
                ask_clusters[cluster_price]["volume"] += ask.value_usd
                ask_clusters[cluster_price]["count"] += 1
                ask_clusters[cluster_price]["prices"].append(ask.price)
        
        # Convert ask clusters to zones
        total_ask_volume = sum(c["volume"] for c in ask_clusters.values()) if ask_clusters else 1
        for cluster_price, data in ask_clusters.items():
            avg_price = sum(data["prices"]) / len(data["prices"]) if data["prices"] else cluster_price
            distance = ((avg_price - mid_price) / mid_price) * 100
            is_major = data["volume"] > total_ask_volume * 0.2 or data["volume"] > 100000
            
            resistance_zones.append(LiquidityZone(
                price=avg_price,
                total_volume_usd=data["volume"],
                side="ask",
                distance_pct=distance,
                order_count=data["count"],
                is_major=is_major
            ))
        
        # Sort by volume (most significant first)
        support_zones.sort(key=lambda z: z.total_volume_usd, reverse=True)
        resistance_zones.sort(key=lambda z: z.total_volume_usd, reverse=True)
        
        return support_zones, resistance_zones
    
    def _generate_suggestions(
        self,
        result: SignalResult,
        bids: List[OrderBookLevel],
        asks: List[OrderBookLevel]
    ) -> Tuple[Optional[TradeSuggestion], Optional[TradeSuggestion]]:
        """Generate trade suggestions for scalp and reversal modes"""
        
        scalp = None
        reversal = None
        mid_price = result.mid_price
        
        if not mid_price or mid_price == 0:
            return None, None
        
        # === SCALP SUGGESTION (based on near-price imbalance) ===
        if result.score >= 20:
            # Bullish - suggest long
            stop_distance = max(result.spread_bps * 3 / 10000, 0.005)  # At least 0.5%
            target_distance = stop_distance * 2  # 2:1 reward/risk
            
            scalp = TradeSuggestion(
                action="LONG",
                mode="scalp",
                entry_price=mid_price,
                target_price=mid_price * (1 + target_distance),
                stop_price=mid_price * (1 - stop_distance),
                confidence=min(result.confidence, 80),
                reason=f"Near-price buying pressure ({result.imbalance_ratio:.2f}x bid imbalance)"
            )
        elif result.score <= -20:
            # Bearish - suggest short
            stop_distance = max(result.spread_bps * 3 / 10000, 0.005)
            target_distance = stop_distance * 2
            
            scalp = TradeSuggestion(
                action="SHORT",
                mode="scalp",
                entry_price=mid_price,
                target_price=mid_price * (1 - target_distance),
                stop_price=mid_price * (1 + stop_distance),
                confidence=min(result.confidence, 80),
                reason=f"Near-price selling pressure ({1/result.imbalance_ratio:.2f}x ask imbalance)"
            )
        
        # === REVERSAL SUGGESTION (based on liquidity zones) ===
        # Find the strongest support and resistance zones
        major_supports = [z for z in result.support_zones if z.is_major]
        major_resistances = [z for z in result.resistance_zones if z.is_major]
        
        # If price is closer to a major support, suggest long at that level
        if major_supports:
            best_support = major_supports[0]
            if best_support.distance_pct < 10:  # Within 10%
                # Find target at nearest major resistance or 2x the distance
                target_price = mid_price * (1 + best_support.distance_pct / 100)
                if major_resistances:
                    target_price = major_resistances[0].price
                
                reversal = TradeSuggestion(
                    action="LONG",
                    mode="reversal",
                    entry_price=best_support.price,
                    target_price=target_price,
                    stop_price=best_support.price * 0.97,  # 3% below support
                    confidence=min(70, best_support.total_volume_usd / 10000),
                    reason=f"Major support zone at ${best_support.price:.6f} (${best_support.total_volume_usd:,.0f} in bids)"
                )
        
        # If price is closer to a major resistance, could suggest short
        if major_resistances and not reversal:
            best_resistance = major_resistances[0]
            if best_resistance.distance_pct < 10:
                target_price = mid_price * (1 - best_resistance.distance_pct / 100)
                if major_supports:
                    target_price = major_supports[0].price
                
                reversal = TradeSuggestion(
                    action="SHORT",
                    mode="reversal",
                    entry_price=best_resistance.price,
                    target_price=target_price,
                    stop_price=best_resistance.price * 1.03,  # 3% above resistance
                    confidence=min(70, best_resistance.total_volume_usd / 10000),
                    reason=f"Major resistance zone at ${best_resistance.price:.6f} (${best_resistance.total_volume_usd:,.0f} in asks)"
                )
        
        return scalp, reversal


# Convenience function for quick analysis
def analyze_orderbook(
    symbol: str,
    bids: List[Tuple[float, float]],  # [(price, qty), ...]
    asks: List[Tuple[float, float]],
    trades: List[Tuple[float, str]] = None
) -> dict:
    """
    Quick analysis function
    
    Args:
        symbol: Trading pair
        bids: List of (price, quantity) tuples
        asks: List of (price, quantity) tuples
        trades: Optional list of (value_usd, side) tuples
        
    Returns:
        Signal result as dict
    """
    bid_levels = [OrderBookLevel(price=p, quantity=q) for p, q in bids]
    ask_levels = [OrderBookLevel(price=p, quantity=q) for p, q in asks]
    
    engine = SignalEngine(symbol)
    result = engine.analyze(bid_levels, ask_levels, trades)
    return result.to_dict()


# Test
if __name__ == "__main__":
    # Simulate bullish order book
    bids = [
        OrderBookLevel(100.0, 500),  # $50,000
        OrderBookLevel(99.9, 300),
        OrderBookLevel(99.8, 1000),  # Big wall
        OrderBookLevel(99.7, 200),
    ]
    asks = [
        OrderBookLevel(100.1, 200),
        OrderBookLevel(100.2, 150),
        OrderBookLevel(100.3, 100),
    ]
    
    engine = SignalEngine("TEST")
    result = engine.analyze(bids, asks)
    
    print(f"Signal: {result.signal.value}")
    print(f"Confidence: {result.confidence:.1f}%")
    print(f"Score: {result.score:.1f}")
    print(f"\nComponents:")
    print(f"  Imbalance: {result.imbalance_score:.1f}")
    print(f"  Weighted Pressure: {result.weighted_pressure_score:.1f}")
    print(f"  Walls: {result.wall_score:.1f}")
    print(f"  Spread: {result.spread_score:.1f}")
    print(f"  Flow: {result.flow_score:.1f}")
    print(f"  Momentum: {result.momentum_score:.1f}")
    print(f"\nReasons:")
    for r in result.reasons:
        print(f"  {r}")
