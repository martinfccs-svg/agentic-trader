import sys; print("MAIN.PY STARTED", file=sys.stderr, flush=True)
def run(loop: bool, cycles: int = 40) -> None:
    print("DEBUG: Starting build()", flush=True)
    try:
        feed, broker, logger, kill, swing, intraday, router = build()
    except Exception as e:
        print(f"FATAL: Failed to build components: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise
    
    print(f"DEBUG: Build complete. Feed type: {type(feed).__name__}", flush=True)
    log.info("agentic-trader v5 starting in %s mode", TRADING_MODE)

    sim = isinstance(feed, SimulatedFeed)
    n = 10_000_000 if loop else cycles
    print(f"DEBUG: Starting loop with n={n}, sim={sim}", flush=True)
    
    for i in range(n):
        print(f"DEBUG: Cycle {i} starting", flush=True)
        try:
            one_cycle(feed, kill, swing, intraday, router)
            print(f"DEBUG: Cycle {i} complete", flush=True)
        except Exception as e:
            print(f"ERROR: Cycle {i} failed: {e}", flush=True)
            import traceback
            traceback.print_exc()
            log.error("Cycle %d failed: %s", i, e, exc_info=True)
            if not loop:
                raise
            time.sleep(5)  # backoff before retry
            continue
        
        if sim:
            feed.step_prices()      # advance synthetic market
        if loop:
            time.sleep(5)
    
    print("DEBUG: Loop complete, flattening positions", flush=True)
    # End of (simulated) session: flatten intraday, keep swing overnight.
    intraday.flatten_all("session end")
    logger.print_scorecard(broker)
    print("DEBUG: Session complete", flush=True)
