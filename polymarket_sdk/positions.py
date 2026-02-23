"""
Position Manager: Track and manage trading positions
Entry/exit logic + rebalancing
"""

import json
from datetime import datetime
from pathlib import Path

class PositionManager:
    def __init__(self, bankroll=60.0, positions_file='positions.json'):
        self.bankroll = bankroll
        self.positions_file = Path(positions_file)
        self.positions = self.load_positions()

        # Risk parameters
        self.MAX_POSITION_SIZE = 0.25  # 25% per position (removed - Kelly handles this)
        self.MAX_TOTAL_EXPOSURE = 1.00  # 100% total exposure allowed (Kelly manages risk)
        self.EDGE_SHRINK_THRESHOLD = 0.20  # Exit if edge shrinks to <20% of original
        self.TIME_DECAY_HOURS = 0  # DISABLED (was 6) - PATIENT ACCUMULATOR: hold to resolution
        self.TIME_DECAY_LOSS_PCT = -0.20  # (unused when TIME_DECAY_HOURS=0)
        self.REBALANCE_THRESHOLD = 0.20  # 20% change triggers rebalance

    def load_positions(self):
        """Load positions from file"""
        if self.positions_file.exists():
            with open(self.positions_file, 'r') as f:
                return json.load(f)
        return []

    def save_positions(self):
        """Save positions to file"""
        with open(self.positions_file, 'w') as f:
            json.dump(self.positions, f, indent=2)

    def get_open_positions(self):
        """Get all open positions"""
        return [p for p in self.positions if p['status'] == 'OPEN']

    def get_total_exposure(self):
        """Calculate total exposure across all positions"""
        return sum(p['size'] for p in self.get_open_positions())

    def can_open_position(self, size):
        """Check if we can open a new position given size"""
        # Kelly criterion already handles position sizing - no arbitrary caps
        # Just check total exposure doesn't exceed bankroll
        current_exposure = self.get_total_exposure()
        if current_exposure + size > self.bankroll * self.MAX_TOTAL_EXPOSURE:
            return False, f"Total exposure would exceed 100% ({current_exposure + size:.2f} / {self.bankroll * self.MAX_TOTAL_EXPOSURE:.2f})"

        return True, "OK"

    def open_position(self, window, bin_range, entry_price, size, token_id, order_id, model_prob=None, edge=None, side="YES", reason="", portfolio_id=None, portfolio_mode=None, portfolio_role=None, portfolio_weight=None):
        """Record a new position (with optional portfolio metadata)"""
        position = {
            'id': len(self.positions) + 1,
            'window': window,
            'bin': bin_range,
            'side': side,  # YES or NO
            'entry_price': float(entry_price),
            'entry_time': datetime.now().isoformat(),
            'size': float(size),
            'token_id': token_id,
            'order_id': order_id,
            'model_prob': float(model_prob) if model_prob else None,
            'entry_edge': float(edge) if edge else None,
            'status': 'OPEN',
            'exit_price': None,
            'exit_time': None,
            'pnl': 0.0,
            'reason': reason,
            # Portfolio metadata (BAND/FULL mode)
            'portfolio_id': portfolio_id,
            'portfolio_mode': portfolio_mode,  # PEAK/BAND/FULL
            'portfolio_role': portfolio_role,  # primary/hedge/diversifier
            'portfolio_weight': float(portfolio_weight) if portfolio_weight else None
        }

        self.positions.append(position)
        self.save_positions()

        print(f"✅ Opened position: {window} {bin_range} @ {entry_price:.4f} (${size:.2f})")
        return position

    def close_position(self, position_id, exit_price, reason=""):
        """Close a position"""
        for position in self.positions:
            if position['id'] == position_id and position['status'] == 'OPEN':
                position['exit_price'] = float(exit_price)
                position['exit_time'] = datetime.now().isoformat()
                position['status'] = 'CLOSED'

                # Calculate P&L
                entry_value = position['size'] / position['entry_price']
                exit_value = entry_value * exit_price
                pnl = exit_value - position['size']
                position['pnl'] = float(pnl)
                position['exit_reason'] = reason

                self.save_positions()

                pnl_pct = (pnl / position['size']) * 100
                print(f"✅ Closed position #{position_id}: {position['window']} {position['bin']}")
                print(f"   Entry: {position['entry_price']:.4f}, Exit: {exit_price:.4f}")
                print(f"   P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
                print(f"   Reason: {reason}")

                return position

        print(f"❌ Position #{position_id} not found or already closed")
        return None

    def should_exit(self, position, current_price, model_prob, hours_to_close):
        """
        Determine if position should be exited

        Edge-based exits: Let winners run to expiry, exit when edge deteriorates

        Returns: (should_exit: bool, reason: str)
        """
        # Calculate entry edge (why we entered)
        entry_model_prob = position.get('model_prob', position['entry_price'] + 0.10)  # Fallback estimate
        entry_edge = entry_model_prob - position['entry_price']

        # Calculate current edge
        current_edge = model_prob - current_price

        # Current P&L
        pnl_pct = (current_price - position['entry_price']) / position['entry_price']

        # 1. EDGE FLIP - Model now says overpriced (CRITICAL EXIT)
        if current_edge < 0:
            return True, f"EDGE_FLIP (was +{entry_edge*100:.1f}%, now {current_edge*100:.1f}%) | P&L: {pnl_pct*100:+.1f}%"

        # 2. EDGE SHRINKAGE - Market caught up, edge <20% of original
        if entry_edge > 0 and current_edge < entry_edge * self.EDGE_SHRINK_THRESHOLD:
            edge_remaining_pct = (current_edge / entry_edge) * 100
            return True, f"EDGE_SHRUNK (was +{entry_edge*100:.1f}%, now {current_edge*100:.1f}%, {edge_remaining_pct:.0f}% remaining) | P&L: {pnl_pct*100:+.1f}%"

        # 3. TIME DECAY - Emergency escape for losing positions near expiry
        if hours_to_close < self.TIME_DECAY_HOURS:
            if pnl_pct < self.TIME_DECAY_LOSS_PCT:
                return True, f"TIME_DECAY ({hours_to_close:.1f}h left, losing {pnl_pct*100:.1f}%)"

        # Otherwise HOLD - let winners run to binary expiry (potentially 100-900% gains!)
        return False, None

    def calculate_rebalance(self, position, new_kelly_fraction):
        """
        Determine if position should be rebalanced

        Returns: (action: str, delta: float)
            action: 'BUY', 'SELL', or 'HOLD'
            delta: dollar amount to adjust
        """
        current_size = position['size']
        target_size = new_kelly_fraction * self.bankroll

        size_change = (target_size - current_size) / current_size

        # Only rebalance if change > 20%
        if abs(size_change) > self.REBALANCE_THRESHOLD:
            delta = target_size - current_size

            if delta > 0:
                return 'BUY', delta
            else:
                return 'SELL', abs(delta)

        return 'HOLD', 0

    def get_summary(self):
        """Get portfolio summary"""
        open_positions = self.get_open_positions()
        closed_positions = [p for p in self.positions if p['status'] == 'CLOSED']

        total_exposure = self.get_total_exposure()
        total_pnl = sum(p.get('pnl', 0) for p in closed_positions)

        return {
            'bankroll': self.bankroll,
            'open_positions': len(open_positions),
            'closed_positions': len(closed_positions),
            'total_exposure': total_exposure,
            'exposure_pct': (total_exposure / self.bankroll) * 100,
            'total_pnl': total_pnl,
            'pnl_pct': (total_pnl / self.bankroll) * 100
        }

if __name__ == "__main__":
    print("="*80)
    print("POSITION MANAGER TEST")
    print("="*80)

    # Initialize
    pm = PositionManager(bankroll=60.0, positions_file='test_positions.json')

    # Test: Open position
    can_open, msg = pm.can_open_position(5.0)
    print(f"\nCan open $5 position? {can_open} - {msg}")

    if can_open:
        position = pm.open_position(
            window="Oct 21-28",
            bin_range="180-199",
            entry_price=0.0965,
            size=5.0,
            token_id="test_token",
            order_id="test_order_1",
            reason="Model edge +25%"
        )

    # Test: Should exit?
    print("\n" + "="*80)
    print("Exit Logic Tests")
    print("="*80)

    test_cases = [
        (0.15, 0.10, 100, "Price up 55% from entry"),
        (0.05, 0.10, 100, "Price down 48% from entry"),
        (0.09, 0.05, 100, "Edge flipped negative"),
        (0.11, 0.15, 5, "Time decay - 5 hours left, losing"),
    ]

    for i, (current_price, model_prob, hours_left, desc) in enumerate(test_cases, 1):
        should_exit, reason = pm.should_exit(position, current_price, model_prob, hours_left)
        print(f"\nTest {i}: {desc}")
        print(f"  Current: {current_price*100:.1f}¢, Model: {model_prob*100:.1f}%, Hours: {hours_left}")
        print(f"  Decision: {'EXIT' if should_exit else 'HOLD'}")
        if reason:
            print(f"  Reason: {reason}")

    # Test: Summary
    print("\n" + "="*80)
    print("Portfolio Summary")
    print("="*80)

    summary = pm.get_summary()
    print(f"\nBankroll: ${summary['bankroll']:.2f}")
    print(f"Open positions: {summary['open_positions']}")
    print(f"Total exposure: ${summary['total_exposure']:.2f} ({summary['exposure_pct']:.1f}%)")
    print(f"Total P&L: ${summary['total_pnl']:+.2f} ({summary['pnl_pct']:+.1f}%)")
