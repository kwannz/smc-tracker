"""信号引擎：SMC 共振 + CEX⟂DEX 背离 + 多庄共识。"""
from .engine import Signal, SignalEngine
from .divergence import DivergenceDetector, DivergenceSignal
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

__all__ = ["Signal", "SignalEngine", "DivergenceDetector", "DivergenceSignal",
           "ConsensusSignal", "CoinPositioning", "WhaleConsensus", "positioning",
           "PositionChange", "WhalePositionTracker",
           "ConfluenceAggregator", "ConfluenceSignal", "TASignal",
           "PumpRadar", "PumpAlert",
           "FlowPredictor", "FlowPrediction", "orderbook_imbalance",
           "SignalEfficacy", "KindEfficacy", "wilson_interval",
           "PositionSize", "compute_position_size",
           "KNNVerdict", "validate_direction",
           "TradeSetup", "build_setups",
           "OrderflowConfirm", "confirm_setup"]
