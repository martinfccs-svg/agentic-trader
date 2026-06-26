import logging
import time

log = logging.getLogger("main")

def main():
    # ... existing setup code ...
    
    cycle_count = 0
    while True:
        cycle_count += 1
        cycle_start = time.time()
        
        try:
            # Log cycle start
            log.info(f"=== CYCLE {cycle_count} START ===")
            
            # Fetch feed data
            feed.update()
            log.info(f"Feed updated: {len(feed.symbols)} symbols")
            
            # Get current prices
            prices = feed.get_prices()
            log.info(f"Prices fetched: {len(prices)} symbols")
            
            # Generate signals
            signals = router.generate_signals(prices)
            if signals:
                log.info(f"Signals generated: {len(signals)} signals")
                for symbol, signal in signals.items():
                    log.info(f"  {symbol}: {signal.type} @ ${signal.price}")
            else:
                log.info("No signals generated")
            
            # Execute orders
            orders = router.execute_signals(signals)
            if orders:
                log.info(f"Orders executed: {len(orders)} orders")
                for order in orders:
                    log.info(f"  {order.side.upper()} {order.quantity} {order.symbol} @ ${order.price}")
            
            # Log P&L
            pnl = broker.get_pnl()
            log.info(f"P&L - Realized: ${pnl['realized']:.2f}, Unrealized: ${pnl['unrealized']:.2f}")
            
            cycle_duration = time.time() - cycle_start
            log.info(f"=== CYCLE {cycle_count} COMPLETE ({cycle_duration:.2f}s) ===\n")
            
        except Exception as e:
            log.error(f"Cycle {cycle_count} error: {e}", exc_info=True)
        
        # Sleep until next cycle
        time.sleep(SCAN_INTERVAL_SECS)
