# pragma pylint: disable=attribute-defined-outside-init

"""
This module load custom strategies
"""
import logging
import tempfile
from base64 import urlsafe_b64decode
from inspect import getfullargspec
from pathlib import Path
from typing import Any, Dict, Optional

from freqtrade.constants import REQUIRED_ORDERTIF, REQUIRED_ORDERTYPES, USERPATH_STRATEGIES
from freqtrade.exceptions import OperationalException
from freqtrade.resolvers import IResolver
from freqtrade.strategy.interface import IStrategy


logger = logging.getLogger(__name__)


class StrategyResolver(IResolver):
    """
    This class contains the logic to load custom strategy class
    """
    object_type = IStrategy
    object_type_str = "Strategy"
    user_subdir = USERPATH_STRATEGIES
    initial_search_path = None

    @staticmethod
    def load_strategy(config: Dict[str, Any] = None) -> IStrategy:
        """
        Load the custom class from config parameter
        :param config: configuration dictionary or None
        """
        config = config or {}

        if not config.get('strategy'):
            raise OperationalException("No strategy set. Please use `--strategy` to specify "
                                       "the strategy class to use.")

        strategy_name = config['strategy']
        strategy: IStrategy = StrategyResolver._load_strategy(
            strategy_name, config=config,
            extra_dir=config.get('strategy_path'))

        if hasattr(strategy, 'ticker_interval') and not hasattr(strategy, 'timeframe'):
            # Assign ticker_interval to timeframe to keep compatibility
            if 'timeframe' not in config:
                logger.warning(
                    "DEPRECATED: Please migrate to using 'timeframe' instead of 'ticker_interval'."
                )
                strategy.timeframe = strategy.ticker_interval

        if strategy._ft_params_from_file:
            # Set parameters from Hyperopt results file
            params = strategy._ft_params_from_file
            strategy.minimal_roi = params.get('roi', strategy.minimal_roi)

            strategy.stoploss = params.get('stoploss', {}).get('stoploss', strategy.stoploss)
            trailing = params.get('trailing', {})
            strategy.trailing_stop = trailing.get('trailing_stop', strategy.trailing_stop)
            strategy.trailing_stop_positive = trailing.get('trailing_stop_positive',
                                                           strategy.trailing_stop_positive)
            strategy.trailing_stop_positive_offset = trailing.get(
                'trailing_stop_positive_offset', strategy.trailing_stop_positive_offset)
            strategy.trailing_only_offset_is_reached = trailing.get(
                'trailing_only_offset_is_reached', strategy.trailing_only_offset_is_reached)

        # Set attributes
        # Check if we need to override configuration
        #             (Attribute name,                    default,     subkey)
        attributes = [("minimal_roi",                     {"0": 10.0}),
                      ("timeframe",                       None),
                      ("stoploss",                        None),
                      ("trailing_stop",                   None),
                      ("trailing_stop_positive",          None),
                      ("trailing_stop_positive_offset",   0.0),
                      ("trailing_only_offset_is_reached", None),
                      ("use_custom_stoploss",             None),
                      ("process_only_new_candles",        None),
                      ("order_types",                     None),
                      ("order_time_in_force",             None),
                      ("stake_currency",                  None),
                      ("stake_amount",                    None),
                      ("protections",                     None),
                      ("startup_candle_count",            None),
                      ("unfilledtimeout",                 None),
                      ("use_sell_signal",                 True),
                      ("sell_profit_only",                False),
                      ("ignore_roi_if_buy_signal",        False),
                      ("sell_profit_offset",              0.0),
                      ("disable_dataframe_checks",        False),
                      ("ignore_buying_expired_candle_after",  0)
                      ]
        for attribute, default in attributes:
            StrategyResolver._override_attribute_helper(strategy, config,
                                                        attribute, default)

        # Loop this list again to have output combined
        for attribute, _ in attributes:
            if attribute in config:
                logger.info("Strategy using %s: %s", attribute, config[attribute])

        StrategyResolver._normalize_attributes(strategy)

        StrategyResolver._strategy_sanity_validations(strategy)
        return strategy

    @staticmethod
    def _override_attribute_helper(strategy, config: Dict[str, Any],
                                   attribute: str, default: Any):
        """
        Override attributes in the strategy.
        Prevalence:
        - Configuration
        - Strategy
        - default (if not None)
        """
        if (attribute in config
                and not isinstance(getattr(type(strategy), attribute, None), property)):
            # Ensure Properties are not overwritten
            setattr(strategy, attribute, config[attribute])
            logger.info("Override strategy '%s' with value in config file: %s.",
                        attribute, config[attribute])
        elif hasattr(strategy, attribute):
            val = getattr(strategy, attribute)
            # None's cannot exist in the config, so do not copy them
            if val is not None:
                config[attribute] = val
        # Explicitly check for None here as other "falsy" values are possible
        elif default is not None:
            setattr(strategy, attribute, default)
            config[attribute] = default

    @staticmethod
    def _normalize_attributes(strategy: IStrategy) -> IStrategy:
        """
        Normalize attributes to have the correct type.
        """
        # Assign deprecated variable - to not break users code relying on this.
        if hasattr(strategy, 'timeframe'):
            strategy.ticker_interval = strategy.timeframe

        # Sort and apply type conversions
        if hasattr(strategy, 'minimal_roi'):
            strategy.minimal_roi = dict(sorted(
                {int(key): value for (key, value) in strategy.minimal_roi.items()}.items(),
                key=lambda t: t[0]))
        if hasattr(strategy, 'stoploss'):
            strategy.stoploss = float(strategy.stoploss)
        return strategy

    @staticmethod
    def _strategy_sanity_validations(strategy):
        if not all(k in strategy.order_types for k in REQUIRED_ORDERTYPES):
            raise ImportError(f"Impossible to load Strategy '{strategy.__class__.__name__}'. "
                              f"Order-types mapping is incomplete.")

        if not all(k in strategy.order_time_in_force for k in REQUIRED_ORDERTIF):
            raise ImportError(f"Impossible to load Strategy '{strategy.__class__.__name__}'. "
                              f"Order-time-in-force mapping is incomplete.")

    @staticmethod
    def _load_strategy(strategy_name: str,
                       config: dict, extra_dir: Optional[str] = None) -> IStrategy:
        """
        Search and loads the specified strategy.
        :param strategy_name: name of the module to import
        :param config: configuration for the strategy
        :param extra_dir: additional directory to search for the given strategy
        :return: Strategy instance or None
        """

        abs_paths = StrategyResolver.build_search_paths(config,
                                                        user_subdir=USERPATH_STRATEGIES,
                                                        extra_dir=extra_dir)

        if ":" in strategy_name:
            logger.info("loading base64 encoded strategy")
            strat = strategy_name.split(":")

            if len(strat) == 2:
                temp = Path(tempfile.mkdtemp("freq", "strategy"))
                name = strat[0] + ".py"

                temp.joinpath(name).write_text(urlsafe_b64decode(strat[1]).decode('utf-8'))
                temp.joinpath("__init__.py").touch()

                strategy_name = strat[0]

                # register temp path with the bot
                abs_paths.insert(0, temp.resolve())

        strategy = StrategyResolver._load_object(
            paths=abs_paths,
            object_name=strategy_name,
            add_source=True,
            kwargs={'config': config},
        )

        if strategy:
            strategy._populate_fun_len = len(getfullargspec(strategy.populate_indicators).args)
            strategy._buy_fun_len = len(getfullargspec(strategy.populate_buy_trend).args)
            strategy._sell_fun_len = len(getfullargspec(strategy.populate_sell_trend).args)
            if any(x == 2 for x in [
                strategy._populate_fun_len,
                strategy._buy_fun_len,
                strategy._sell_fun_len
            ]):
                strategy.INTERFACE_VERSION = 1

            return strategy

        raise OperationalException(
            f"Impossible to load Strategy '{strategy_name}'. This class does not exist "
            "or contains Python code errors."
        )
