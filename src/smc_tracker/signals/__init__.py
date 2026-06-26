"""信号引擎：SMC 共振 + CEX⟂DEX 背离 + 多庄共识。"""
from .engine import Signal, SignalEngine
from .divergence import DivergenceDetector, DivergenceSignal, pred_kind
from .consensus import CoinPositioning, ConsensusSignal, WhaleConsensus, positioning
from .position_tracker import PositionChange, WhalePositionTracker
from .confluence import ConfluenceAggregator, ConfluenceSignal
from .ta_signal import TASignal
from .pump_radar import PumpRadar, PumpAlert
from .flow_predictor import FlowPredictor, FlowPrediction, orderbook_imbalance
from .efficacy import SignalEfficacy, KindEfficacy, wilson_interval
from .risk import PositionSize, compute_position_size
from .knn_validator import KNNVerdict, validate_direction
from .trade_setup import TradeSetup, build_setups
from .orderflow_confirm import OrderflowConfirm, confirm_setup
from .coin_profile import CoinSignalProfile, build_profile, signal_asset_class
from .forward_confirm import forward_mult, apply_forward
from .funding_extreme import funding_extreme_signal
from .oi_velocity import oi_directional_velocity
from .harmonic_dedup import setup_fingerprint, SetupDedup
from .harmonic_review import build_harmonic_predictions
# C.1: 微观结构盘口信号三件套（OFI + queue_imbalance + micro_price）
from .microprice import OFITracker, queue_imbalance, micro_price, ofi_delta
# 共享聚合 helper：读 11 张信号表 → 统一行结构 → 按 ts 倒序
from .all_signals import collect_all_signals
# MTF 分层入场决策(顶12h+1d定向/中1h+4h确认/底5m+15m触发)
from .mtf_confluence import mtf_decision, fmt_mtf

__all__ = ["Signal", "SignalEngine", "DivergenceDetector", "DivergenceSignal", "pred_kind",
           "ConsensusSignal", "CoinPositioning", "WhaleConsensus", "positioning",
           "PositionChange", "WhalePositionTracker",
           "ConfluenceAggregator", "ConfluenceSignal", "TASignal",
           "PumpRadar", "PumpAlert",
           "FlowPredictor", "FlowPrediction", "orderbook_imbalance",
           "SignalEfficacy", "KindEfficacy", "wilson_interval",
           "PositionSize", "compute_position_size",
           "KNNVerdict", "validate_direction",
           "TradeSetup", "build_setups",
           "OrderflowConfirm", "confirm_setup",
           "CoinSignalProfile", "build_profile", "signal_asset_class",
           "forward_mult", "apply_forward",
           "funding_extreme_signal", "oi_directional_velocity",
           "setup_fingerprint", "SetupDedup", "build_harmonic_predictions",
           "OFITracker", "queue_imbalance", "micro_price", "ofi_delta",
           "collect_all_signals", "mtf_decision", "fmt_mtf"]
