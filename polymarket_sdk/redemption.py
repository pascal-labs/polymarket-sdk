#!/usr/bin/env python3
"""
Enhanced Polymarket Winnings Redeemer - Redeems WINNING positions only

Features:
- Redeems only WINNING positions (losers have no payout and would fail)
- Automatically detects negRisk vs standard markets
- Handles wrapped collateral redemption and unwrapping
- Batch resolution checking for efficiency
- Comprehensive error handling
- Uses Alchemy API if available for reliable transaction submission

NOTE: Losing positions cannot be redeemed (they have $0 payout) and are automatically skipped
"""

from web3 import Web3
from typing import List, Tuple, Optional, Dict
import os
import time
import requests
from dotenv import load_dotenv
from eth_account import Account

# Track positions we've already attempted (to avoid infinite retry loops)
# Key: condition_id, Value: timestamp of last attempt
_attempted_positions: Dict[str, float] = {}
ATTEMPT_COOLDOWN = 600  # 10 minutes before retrying same position after confirmed failure
RATE_LIMIT_COOLDOWN = 15  # 15 seconds before retrying after rate limit

# Track positions we've SUCCESSFULLY redeemed (never retry these)
# Key: condition_id, Value: True
_redeemed_positions: Dict[str, bool] = {}

# Load environment
load_dotenv()

# Polygon RPC endpoints
POLYGON_RPC = "https://polygon-rpc.com"  # Official RPC
RPC_POOL = [
    "https://polygon-rpc.com",                     # Official Polygon RPC
    "https://rpc-mainnet.matic.network",           # Matic official
    "https://rpc-mainnet.maticvigil.com",         # MaticVigil
    "https://polygon-mainnet.public.blastapi.io",  # BlastAPI
    "https://1rpc.io/matic",                       # 1RPC
]

# IMPORTANT: Use Alchemy if available (most reliable, no fee cap)
_alchemy_url = os.getenv("ALCHEMY_POLYGON_URL", "")
if _alchemy_url:
    # Support both full URL and bare API key
    if _alchemy_url.startswith("http"):
        RPC_POOL.insert(0, _alchemy_url)
    else:
        RPC_POOL.insert(0, f"https://polygon-mainnet.g.alchemy.com/v2/{_alchemy_url}")
elif os.getenv("POLYGON_RPC"):
    RPC_POOL.insert(0, os.getenv("POLYGON_RPC"))

# Contract addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC.e
USDC_N = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"  # Native USDC

# NegRisk contracts on Polygon
NEGRISK_CONTRACTS = {
    "ctf": "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
    "negRiskAdapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "negRiskCtfExchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "negRiskWrappedCollateral": "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2",
}

ZERO32 = b"\x00" * 32

# CTF ABI (minimal for redemption)
CTF_ABI = [
    {
        "inputs": [
            {"internalType": "contract IERC20", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256", "name": "index", "type": "uint256"}
        ],
        "name": "payoutNumerators",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id", "type": "uint256"}
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Wrapped Collateral ABI
WRAPPED_COLLATERAL_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_to", "type": "address"},
            {"internalType": "uint256", "name": "_amount", "type": "uint256"}
        ],
        "name": "unwrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class EnhancedWinningsRedeemer:
    """Enhanced redeemer that handles both standard and negRisk markets"""

    def __init__(self, private_key: str, rpc_url: str = None):
        """Initialize redeemer with Web3 connection"""
        self.private_key_hex = private_key if private_key.startswith("0x") else "0x" + private_key

        # Try multiple RPCs to find a working one
        if rpc_url:
            test_rpcs = [rpc_url]
        else:
            test_rpcs = RPC_POOL

        self.w3 = None
        for url in test_rpcs:
            try:
                print(f"   Testing RPC: {url}...")
                test_w3 = Web3(Web3.HTTPProvider(url))
                if test_w3.is_connected():
                    # Test with actual call
                    block = test_w3.eth.block_number
                    gas_price = test_w3.eth.gas_price

                    self.w3 = test_w3
                    self.rpc_url = url
                    print(f"   ✅ Connected via {url} (block={block}, gas={gas_price/1e9:.1f} gwei)")
                    break
            except Exception as e:
                print(f"   ❌ Failed: {str(e)[:50]}")
                continue

        if not self.w3:
            raise RuntimeError("Failed to connect to any Polygon RPC")

        self.account = Account.from_key(self.private_key_hex)

        # Setup contracts
        self.ctf_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI
        )

        self.wrapped_collateral_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(NEGRISK_CONTRACTS["negRiskWrappedCollateral"]),
            abi=WRAPPED_COLLATERAL_ABI
        )

        print(f"✅ Connected to Polygon")
        print(f"   Wallet: {self.account.address}")

    def _get_balance_file_path(self):
        """Get the path to balance_history.csv (same dir as auto_redeem.py)"""
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))  # src/
        project_root = os.path.dirname(script_dir)  # btc_arb/
        return os.path.join(project_root, "balance_history.csv")

    def _log_individual_trade(self, balance: float, gained: float, is_winner: bool):
        """Log a single trade to balance_history.csv.

        Each trade gets its own entry for accurate WR and R tracking.
        positions_redeemed=1 for each individual trade.
        gained is positive for wins, negative for losses.
        """
        import csv
        import os
        from datetime import datetime

        balance_file = self._get_balance_file_path()
        os.makedirs(os.path.dirname(balance_file), exist_ok=True)

        file_exists = os.path.exists(balance_file)

        with open(balance_file, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'balance_usdc', 'gained_usdc', 'positions_redeemed'])
            writer.writerow([
                datetime.now().isoformat(),
                f"{balance:.2f}",
                f"{gained:.2f}",
                1  # Each trade logged individually
            ])

        outcome = "WIN" if is_winner else "LOSS"
        print(f"   📝 Logged {outcome}: ${gained:+.2f} -> Balance: ${balance:.2f}")

    def _log_balance(self, balance: float, gained: float, positions_redeemed: int):
        """Log balance to CSV for tracking over time.

        DEPRECATED: Use _log_individual_trade for each trade instead.
        This is kept for backward compatibility but should not be called
        when individual trade logging is active.

        Only logs if balance actually changed from last entry (avoids duplicates).
        """
        import csv
        import os
        from datetime import datetime

        balance_file = self._get_balance_file_path()
        os.makedirs(os.path.dirname(balance_file), exist_ok=True)

        file_exists = os.path.exists(balance_file)

        # Check last balance to avoid duplicate logging
        if file_exists:
            try:
                with open(balance_file, 'r') as f:
                    lines = f.readlines()
                    if len(lines) > 1:  # Has data rows
                        last_line = lines[-1].strip()
                        last_balance = float(last_line.split(',')[1])
                        # Skip if balance unchanged (duplicate entry)
                        if abs(last_balance - balance) < 0.01:
                            print(f"   ⏸️  Balance unchanged (${balance:.2f}), skipping duplicate log entry")
                            return
            except Exception as e:
                print(f"   ⚠️  Could not check last balance: {e}")

        with open(balance_file, 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'balance_usdc', 'gained_usdc', 'positions_redeemed'])
            writer.writerow([
                datetime.now().isoformat(),
                f"{balance:.2f}",
                f"{gained:.2f}",
                positions_redeemed
            ])

    def get_redeemable_positions(self) -> List[dict]:
        """Get list of redeemable positions from Polymarket API"""
        url = f"https://data-api.polymarket.com/positions"
        params = {
            'user': self.account.address.lower(),
            'redeemable': 'true'
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            positions_data = response.json()
        except Exception as e:
            print(f"❌ Failed to fetch positions: {e}")
            return []

        # Parse positions
        redeemable = []
        for pos in positions_data:
            condition_id = pos.get('conditionId')
            outcome = pos.get('outcome')  # 'Yes' or 'No'
            size = float(pos.get('size', 0))
            asset = pos.get('asset')  # Token ID

            if condition_id and outcome and size > 0:
                # Try to get slug from multiple possible locations in API response
                market_slug = 'unknown'
                if 'slug' in pos:
                    market_slug = pos['slug']
                elif 'market' in pos and isinstance(pos['market'], dict):
                    market_slug = pos['market'].get('slug', 'unknown')
                elif 'eventSlug' in pos:
                    market_slug = pos['eventSlug']

                redeemable.append({
                    'condition_id': condition_id,
                    'outcome': outcome.upper(),
                    'shares': size,
                    'asset': asset,
                    'market_slug': market_slug,
                })

        return redeemable

    def check_resolution(self, condition_id: str) -> Tuple[bool, str]:
        """Check if condition is resolved and get winner"""
        try:
            condition_bytes = bytes.fromhex(condition_id[2:])

            # Check payouts for binary outcomes
            payout_0 = self.ctf_contract.functions.payoutNumerators(condition_bytes, 0).call()
            payout_1 = self.ctf_contract.functions.payoutNumerators(condition_bytes, 1).call()

            if payout_0 > 0:
                return True, 'YES'
            elif payout_1 > 0:
                return True, 'NO'
            else:
                return False, None
        except Exception:
            return False, None

    def redeem_position(self, position: dict, is_negrisk: bool = False) -> Optional[str]:
        """Redeem a single position"""
        condition_id = position['condition_id']
        shares = position['shares']
        outcome = position.get('outcome', 'Unknown')

        # Calculate the position ID to track what we're redeeming
        condition_bytes = bytes.fromhex(condition_id[2:])

        # For debugging: show what position ID we're trying to redeem
        if position.get('asset'):
            print(f"\n💰 Redeeming {shares:.2f} {outcome} shares (Position ID: {position['asset']})")
        else:
            print(f"\n💰 Redeeming {shares:.2f} {outcome} shares...")

        # Setup parameters
        parent_collection = ZERO32
        index_sets = [1, 2]  # Binary outcomes - redeem both for negRisk markets

        # For negRisk markets, ALWAYS try wrapped collateral first
        # This is what actually works based on our testing
        collateral_options = []
        if is_negrisk:
            collateral_options = [
                (NEGRISK_CONTRACTS["negRiskWrappedCollateral"], "Wrapped Collateral"),
                (USDC_E, "USDC.e (fallback)")
            ]
            print(f"   🔄 NegRisk market detected - will try wrapped collateral first")
        else:
            collateral_options = [
                (USDC_E, "Standard USDC.e")
            ]

        # Get nonce - check for pending transactions (with delay to avoid rate limits)
        time.sleep(1)
        nonce_pending = self.w3.eth.get_transaction_count(self.account.address, "pending")
        time.sleep(0.5)
        nonce_confirmed = self.w3.eth.get_transaction_count(self.account.address, "latest")

        if nonce_pending > nonce_confirmed:
            stuck_count = nonce_pending - nonce_confirmed
            print(f"   ⚠️  {stuck_count} stuck transaction(s) detected! Auto-cancelling...")

            # Cancel each stuck nonce
            for stuck_nonce in range(nonce_confirmed, nonce_pending):
                try:
                    # Get current gas price for cancel tx
                    gas_price = self.w3.eth.gas_price
                    base_fee = self.w3.eth.get_block('latest')['baseFeePerGas']

                    # Use high gas to guarantee replacement
                    cancel_priority = self.w3.to_wei(300, 'gwei')
                    cancel_max_fee = int(base_fee * 3) + cancel_priority

                    cancel_tx = {
                        'to': self.account.address,  # Send to self
                        'value': 0,
                        'gas': 21000,
                        'maxFeePerGas': cancel_max_fee,
                        'maxPriorityFeePerGas': cancel_priority,
                        'nonce': stuck_nonce,
                        'chainId': 137
                    }

                    signed = self.account.sign_transaction(cancel_tx)
                    raw_tx = signed.rawTransaction if hasattr(signed, 'rawTransaction') else signed.raw_transaction
                    tx_hash = self.w3.eth.send_raw_transaction(raw_tx)

                    print(f"   🗑️  Cancelling nonce {stuck_nonce}: {tx_hash.hex()[:20]}...")

                    # Wait briefly for cancel to confirm
                    try:
                        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
                        if receipt.status == 1:
                            print(f"   ✅ Nonce {stuck_nonce} cancelled!")
                        else:
                            print(f"   ❌ Cancel failed for nonce {stuck_nonce}")
                    except:
                        print(f"   ⏳ Cancel tx sent, continuing...")

                except Exception as e:
                    error_msg = str(e)
                    if "replacement transaction underpriced" in error_msg.lower():
                        print(f"   ⚠️  Nonce {stuck_nonce}: need even higher gas to replace")
                    elif "already known" in error_msg.lower():
                        print(f"   ⚠️  Nonce {stuck_nonce}: cancel already pending")
                    else:
                        print(f"   ❌ Failed to cancel nonce {stuck_nonce}: {str(e)[:50]}")

            # Re-check nonce after cancellations
            time.sleep(2)
            nonce_confirmed = self.w3.eth.get_transaction_count(self.account.address, "latest")

        nonce = nonce_confirmed
        print(f"   📝 Using nonce: {nonce}")

        # Try each collateral option until one works
        collateral_token = None
        gas_limit = 300000  # Default

        for token_address, token_name in collateral_options:
            try:
                time.sleep(1)  # Avoid RPC rate limits
                print(f"   Trying gas estimate with {token_name}...")
                gas_estimate = self.ctf_contract.functions.redeemPositions(
                    Web3.to_checksum_address(token_address),
                    parent_collection,
                    condition_bytes,
                    index_sets
                ).estimate_gas({"from": self.account.address})

                gas_limit = int(gas_estimate * 1.3)  # 30% buffer for safety
                collateral_token = token_address
                print(f"   ✅ Gas estimate successful: {gas_estimate} gas")
                break  # Success - use this collateral

            except Exception as e:
                error_msg = str(e)[:100]
                if "execution reverted" in error_msg:
                    print(f"   ❌ {token_name} reverted")
                else:
                    print(f"   ⚠️ {token_name} failed: {error_msg}")
                continue

        if not collateral_token:
            print(f"   ❌ All collateral options failed - position may already be redeemed")
            return ('failed', None)

        # Build transaction with DYNAMIC gas pricing based on network conditions
        try:
            gas_price = self.w3.eth.gas_price
        except:
            # Fallback if RPC fails to provide gas price
            gas_price = self.w3.to_wei(150, 'gwei')

        # Check network congestion level
        gas_gwei = gas_price / 1e9
        if gas_gwei > 1000:
            congestion = "🔥 EXTREME"
            boost_mult = 1.2  # Less boost needed when already high
        elif gas_gwei > 200:
            congestion = "⚠️ HIGH"
            boost_mult = 1.3
        else:
            congestion = "✅ NORMAL"
            boost_mult = 1.5

        # Dynamic: use network price with boost, no artificial cap
        final_gas_price = int(gas_price * boost_mult)
        min_gas_price = self.w3.to_wei(50, 'gwei')
        final_gas_price = max(final_gas_price, min_gas_price)

        print(f"   ⛽ Network: {gas_gwei:.0f} gwei ({congestion}) -> using {final_gas_price/1e9:.0f} gwei")

        # Use EIP-1559 transaction format for better reliability
        try:
            # Try modern transaction format
            base_fee = self.w3.eth.get_block('latest')['baseFeePerGas']
            # Dynamic priority based on congestion
            if base_fee / 1e9 > 1000:
                max_priority_fee = self.w3.to_wei(200, 'gwei')
                base_mult = 2.0  # Lower mult when base is extreme
            else:
                max_priority_fee = self.w3.to_wei(100, 'gwei')
                base_mult = 3.0
            max_fee = int(base_fee * base_mult) + max_priority_fee

            # Guard: total tx fee must stay under 1 MATIC (public RPC cap)
            # total_fee = gas_limit * max_fee; cap at 0.95 MATIC to be safe
            max_total_fee = self.w3.to_wei(0.95, 'ether')
            fee_cap_max_fee = max_total_fee // gas_limit
            if max_fee > fee_cap_max_fee:
                print(f"   ⚠️  Fee cap: {max_fee/1e9:.0f} gwei would exceed 1 MATIC, clamping to {fee_cap_max_fee/1e9:.0f} gwei")
                max_fee = fee_cap_max_fee
                max_priority_fee = min(max_priority_fee, max_fee // 2)

            print(f"   📈 EIP-1559: base={base_fee/1e9:.0f}, maxFee={max_fee/1e9:.0f} gwei, priority={max_priority_fee/1e9:.0f} gwei")

            txn = self.ctf_contract.functions.redeemPositions(
                Web3.to_checksum_address(collateral_token),
                parent_collection,
                condition_bytes,
                index_sets
            ).build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": gas_limit,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": max_priority_fee,
                "chainId": 137
            })
        except:
            # Fallback to legacy transaction
            print(f"   📊 Using legacy gas format")
            txn = self.ctf_contract.functions.redeemPositions(
                Web3.to_checksum_address(collateral_token),
                parent_collection,
                condition_bytes,
                index_sets
            ).build_transaction({
                "from": self.account.address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": final_gas_price,
                "chainId": 137
            })

        # Sign and send with better error handling and debugging
        try:
            signed_txn = self.account.sign_transaction(txn)
            # Handle different web3.py versions
            raw_tx = signed_txn.rawTransaction if hasattr(signed_txn, 'rawTransaction') else signed_txn.raw_transaction
            print(f"   📦 Signed tx size: {len(raw_tx)} bytes")

            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            hex_hash = tx_hash.hex()

            # Immediately check if transaction made it to mempool
            try:
                tx_data = self.w3.eth.get_transaction(tx_hash)
                print(f"   ✅ Transaction in mempool: {hex_hash}")
            except:
                print(f"   ⚠️  Transaction sent: {hex_hash}")
        except Exception as send_error:
            error_msg = str(send_error)
            if "already known" in error_msg.lower():
                print(f"   ⚠️  Transaction already submitted (may be pending)")
                return ('pending', None)
            elif "nonce too low" in error_msg.lower():
                print(f"   ⚠️  Nonce too low - transaction may have already been mined")
                return ('pending', None)  # Might have succeeded
            elif "insufficient funds" in error_msg.lower():
                print(f"   ❌ Insufficient MATIC for gas fees!")
                return ('failed', None)
            elif "replacement transaction underpriced" in error_msg.lower():
                print(f"   ⚠️  Need higher gas to replace pending transaction")
                return ('pending', None)
            else:
                print(f"   ❌ Failed to send transaction: {error_msg[:200]}")
                return ('failed', None)

        # Wait for confirmation
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                # Verify position was actually redeemed by checking balance
                time.sleep(2)  # Give blockchain a moment to update

                # Check if position balance is now 0
                try:
                    pos_id = int(position.get('position_id') or position.get('asset', 0))
                    if pos_id:
                        new_balance = self.ctf_contract.functions.balanceOf(
                            self.account.address,
                            pos_id
                        ).call()

                        if new_balance == 0:
                            print(f"   ✅ Redeemed successfully! Position cleared from wallet.")
                        else:
                            print(f"   ⚠️  Transaction succeeded but position still shows {new_balance} shares")
                except:
                    print(f"   ✅ Transaction confirmed!")

                return ('confirmed', hex_hash)  # Return tuple: (status, hash)
            else:
                print(f"   ❌ Transaction failed")
                return ('failed', None)
        except Exception as e:
            print(f"   ⚠️  Timeout waiting for receipt (transaction likely pending)")
            print(f"   🔍 Check transaction status: https://polygonscan.com/tx/{hex_hash}")

            # Try to check if position was redeemed anyway
            time.sleep(5)  # Give it a moment
            try:
                # Note: position_id might not be set if we're in timeout path
                # Try to get it from the asset field instead
                if position.get('position_id'):
                    pos_id = int(position['position_id'])
                elif position.get('asset'):
                    pos_id = int(position['asset'])
                else:
                    print(f"   ⏳ Cannot verify balance (no position ID available)")
                    return ('pending', hex_hash)

                new_balance = self.ctf_contract.functions.balanceOf(
                    self.account.address,
                    pos_id
                ).call()

                if new_balance == 0:
                    print(f"   ✅ Position cleared! (redemption succeeded despite timeout)")
                    return ('confirmed', hex_hash)  # Actually succeeded
                else:
                    # Convert from micro-units to human-readable shares
                    shares_readable = new_balance / 1e6
                    print(f"   ⏳ Position still shows {shares_readable:.2f} shares (transaction may be pending)")
                    return ('pending', hex_hash)
            except Exception as e:
                print(f"   ⏳ Could not check balance: {str(e)[:100]}")

            return ('pending', hex_hash)  # Unknown state - don't count as success

    def unwrap_collateral(self) -> bool:
        """Unwrap any wrapped collateral to USDC.e"""
        # Check wrapped collateral balance
        wrapped_balance = self.wrapped_collateral_contract.functions.balanceOf(
            Web3.to_checksum_address(self.account.address)
        ).call()

        if wrapped_balance == 0:
            return True

        wrapped_usdc = wrapped_balance / 1e6
        print(f"\n📦 Unwrapping {wrapped_usdc:.2f} wrapped collateral to USDC.e...")

        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        gas_price = self.w3.eth.gas_price

        try:
            # Estimate gas
            gas_estimate = self.wrapped_collateral_contract.functions.unwrap(
                self.account.address,  # Send to self
                wrapped_balance
            ).estimate_gas({"from": self.account.address})

            gas_limit = int(gas_estimate * 1.25)

        except Exception as e:
            print(f"   ❌ Gas estimation failed: {e}")
            return False

        # Build transaction
        txn = self.wrapped_collateral_contract.functions.unwrap(
            self.account.address,
            wrapped_balance
        ).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": int(gas_price * 1.1),
            "chainId": 137
        })

        # Sign and send with better error handling and debugging
        try:
            signed_txn = self.account.sign_transaction(txn)
            # Handle different web3.py versions
            raw_tx = signed_txn.rawTransaction if hasattr(signed_txn, 'rawTransaction') else signed_txn.raw_transaction
            print(f"   📦 Signed tx size: {len(raw_tx)} bytes")

            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            hex_hash = tx_hash.hex()

            # Immediately check if transaction made it to mempool
            try:
                tx_data = self.w3.eth.get_transaction(tx_hash)
                print(f"   ✅ Transaction in mempool: {hex_hash}")
            except:
                print(f"   ⚠️  Transaction sent: {hex_hash}")
        except Exception as send_error:
            error_msg = str(send_error)
            if "already known" in error_msg.lower():
                print(f"   ⚠️  Transaction already submitted (may be pending)")
                return None
            elif "nonce too low" in error_msg.lower():
                print(f"   ⚠️  Nonce too low - transaction may have already been mined")
                return None
            elif "insufficient funds" in error_msg.lower():
                print(f"   ❌ Insufficient MATIC for gas fees!")
                return None
            elif "replacement transaction underpriced" in error_msg.lower():
                print(f"   ⚠️  Need higher gas to replace pending transaction")
                return None
            else:
                print(f"   ❌ Failed to send transaction: {error_msg[:200]}")
                return None

        # Wait for confirmation
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                print(f"   ✅ Unwrapped {wrapped_usdc:.2f} USDC.e successfully!")
                return True
            else:
                print(f"   ❌ Unwrap transaction failed")
                return False
        except Exception as e:
            print(f"   ⚠️  Timeout: {e}")
            return False

    def redeem_all(self) -> int:
        """Main redemption flow"""
        print("\n🔍 Checking for redeemable positions...")

        # Track initial USDC balance to show gains
        usdc_e_contract = self.w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=[
            {
                "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            }
        ])
        initial_usdc = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6
        print(f"💰 Initial USDC.e balance: ${initial_usdc:.2f}")

        # Get positions
        positions = self.get_redeemable_positions()

        if not positions:
            print("   ✅ No positions to redeem")
            return 0

        print(f"\n📋 Found {len(positions)} redeemable position(s):")
        for i, pos in enumerate(positions, 1):
            print(f"   {i}. {pos['outcome']} ({pos['shares']:.2f} shares) - {pos['market_slug']}")

        # Check resolutions
        redeemed_count = 0
        wins_redeemed = 0
        losses_redeemed = 0
        used_negrisk = False

        for position in positions:
            condition_id = position['condition_id']

            # Skip if we've already SUCCESSFULLY redeemed this position (permanent)
            if _redeemed_positions.get(condition_id):
                print(f"\n✅ Skipping {condition_id[:10]}... - already redeemed successfully")
                continue

            # Skip if we've attempted this position recently (cooldown)
            last_attempt = _attempted_positions.get(condition_id, 0)
            if time.time() - last_attempt < ATTEMPT_COOLDOWN:
                remaining = int(ATTEMPT_COOLDOWN - (time.time() - last_attempt))
                print(f"\n⏸️  Skipping {condition_id[:10]}... - attempted recently, cooldown {remaining}s")
                continue

            # Check resolution - retry a few times on rate limit
            is_resolved, winner = False, None
            for attempt in range(3):
                is_resolved, winner = self.check_resolution(position['condition_id'])
                if is_resolved:
                    break
                if attempt < 2:
                    print(f"   ⏳ Resolution check failed, retry {attempt+1}/3...")
                    time.sleep(3)

            # If API says it's redeemable, trust it - try redemption anyway
            if not is_resolved:
                print(f"\n⚠️  Could not verify resolution for {position['condition_id'][:10]}... - trying anyway")
                # Assume resolved, determine win/loss after redemption
                winner = None  # Will check based on balance change

            # Normalize outcome: API returns UP/DOWN, resolution returns YES/NO
            # UP = YES (price up), DOWN = NO (price down)
            outcome = position['outcome'].upper()
            if outcome == 'UP':
                outcome_normalized = 'YES'
            elif outcome == 'DOWN':
                outcome_normalized = 'NO'
            else:
                outcome_normalized = outcome  # Already YES/NO

            # If we know the winner, check if we won
            if winner is not None:
                is_winner = outcome_normalized == winner
                if is_winner:
                    print(f"\n💰 Redeeming WINNING position {position['condition_id'][:10]}...")
                else:
                    print(f"\n🗑️  LOSING position {position['condition_id'][:10]}... (no payout)")
            else:
                is_winner = None  # Unknown until we see balance change
                print(f"\n❓ Redeeming position {position['condition_id'][:10]}... (win/loss unknown)")

            # Check if negRisk market - these are multi-outcome markets that use wrapped collateral
            # NegRisk markets typically include:
            # - Tweet count markets (buckets like "260-279")
            # - Multi-outcome markets (more than YES/NO)
            # - Markets that failed standard USDC redemption in the past
            market_slug = position.get('market_slug', '').lower()
            outcome = position.get('outcome', '')

            is_negrisk = (
                'tweet' in market_slug or
                'bucket' in market_slug or
                'range' in market_slug or
                'multi' in market_slug or
                '-' in outcome  # Bucket ranges like "260-279"
            )

            print(f"   🔍 Market slug: {market_slug}")
            print(f"   🔍 Outcome: {outcome}")
            print(f"   🔍 NegRisk detected: {is_negrisk}")

            # Get USDC balance BEFORE this redemption
            usdc_before = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6

            # Try to redeem on-chain first - track if rate limited
            redemption_rate_limited = False
            try:
                self.redeem_position(position, is_negrisk)
            except Exception as e:
                error_str = str(e).lower()
                if 'rate limit' in error_str or 'too many requests' in error_str or '-32090' in error_str:
                    redemption_rate_limited = True
                print(f"   ⚠️ Redemption attempt error: {str(e)[:50]}")

            # Check if position actually cleared from wallet
            time.sleep(2)  # Give blockchain time to update
            balance_check_success = False
            try:
                pos_id = int(position.get('position_id') or position.get('asset', 0))
                remaining_balance = self.ctf_contract.functions.balanceOf(
                    self.account.address,
                    pos_id
                ).call()
                balance_check_success = True  # We got a real answer
            except Exception as e:
                error_str = str(e).lower()
                if 'rate limit' in error_str or 'too many requests' in error_str or '-32090' in error_str:
                    redemption_rate_limited = True
                print(f"   ⚠️ Balance check failed (rate limited): {str(e)[:30]}")
                remaining_balance = -1  # Unknown, don't assume

            if remaining_balance == 0:
                # Position is ACTUALLY gone - now log it
                print(f"   ✅ Position cleared from wallet!")

                # Get balance after to determine win/loss if unknown
                try:
                    usdc_after = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6
                except:
                    usdc_after = usdc_before  # Assume no change if check fails

                # If we didn't know win/loss, determine from balance change
                if is_winner is None:
                    balance_change = usdc_after - usdc_before
                    is_winner = balance_change > 0.5  # Won if balance increased
                    print(f"   📊 Balance change: ${balance_change:+.2f} -> {'WIN' if is_winner else 'LOSS'}")

                # Use actual balance delta for logging
                balance_delta = usdc_after - usdc_before

                if is_winner:
                    self._log_individual_trade(usdc_after, balance_delta, is_winner=True)
                    wins_redeemed += 1
                else:
                    # Losses: log with gained_usdc=0, stats calculation uses balance delta
                    # The actual loss is detected by comparing consecutive balances in CSV
                    print(f"   💀 Loss - balance: ${usdc_after:.2f}")
                    self._log_individual_trade(usdc_after, 0, is_winner=False)
                    losses_redeemed += 1

                # NOW mark as successfully redeemed
                _redeemed_positions[condition_id] = True
                _attempted_positions[condition_id] = time.time()  # Set cooldown
                redeemed_count += 1
                if is_negrisk:
                    used_negrisk = True
            elif remaining_balance > 0:
                # Position confirmed still in wallet
                if redemption_rate_limited:
                    # Rate limit caused failure - use SHORT cooldown
                    _attempted_positions[condition_id] = time.time() - ATTEMPT_COOLDOWN + RATE_LIMIT_COOLDOWN
                    print(f"   🔄 Rate limited - will retry this position in {RATE_LIMIT_COOLDOWN}s")
                else:
                    # Actual failure (not rate limit) - use full cooldown
                    print(f"   ⚠️ Position still in wallet ({remaining_balance} shares) - will retry in {ATTEMPT_COOLDOWN}s")
                    _attempted_positions[condition_id] = time.time()  # Set full cooldown
            else:
                # Balance check failed (rate limited) - set SHORT cooldown (not zero!)
                # This prevents hammering while still retrying sooner than full cooldown
                _attempted_positions[condition_id] = time.time() - ATTEMPT_COOLDOWN + RATE_LIMIT_COOLDOWN
                print(f"   🔄 Rate limited - will retry this position in {RATE_LIMIT_COOLDOWN}s")

        # If we used negRisk, try to unwrap collateral
        if used_negrisk:
            time.sleep(3)  # Wait for blockchain state
            usdc_before_unwrap = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6
            self.unwrap_collateral()
            time.sleep(2)
            usdc_after_unwrap = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6

            # For negRisk, we can't easily track individual trades since unwrapping is batched
            # Log the total as one entry (better than nothing)
            negRisk_gained = usdc_after_unwrap - usdc_before_unwrap
            if abs(negRisk_gained) > 0.01:
                # Count how many negRisk positions we redeemed
                negRisk_count = sum(1 for p in positions if 'tweet' in p.get('market_slug', '').lower() or '-' in p.get('outcome', ''))
                print(f"   📝 Logging {negRisk_count} negRisk positions as batch (${negRisk_gained:.2f})")
                # Log as individual entries if we can identify them, otherwise batch
                # For now, just log what we gained
                if negRisk_gained > 0:
                    self._log_individual_trade(usdc_after_unwrap, negRisk_gained, True)

        # Check final USDC balance to show gains
        if redeemed_count > 0:
            time.sleep(2)  # Give blockchain a moment to update
            final_usdc = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6
            usdc_gained = final_usdc - initial_usdc

            print(f"\n💵 Final USDC.e balance: ${final_usdc:.2f} (net gained ${usdc_gained:.2f})")

            if usdc_gained > 0:
                print(f"   ✅ Successfully redeemed ${usdc_gained:.2f} USDC from winning positions")
            elif usdc_gained == 0:
                print(f"   💀 No USDC gained (all redeemed positions were losers)")
            else:
                print(f"   ⚠️  Unexpected: USDC decreased by ${-usdc_gained:.2f}")

            # Final verification: check if any positions remain
            print(f"\n🔍 Final wallet check...")
            remaining_positions = 0
            for position in positions:
                try:
                    pos_id = int(position.get('position_id') or position.get('asset', 0))
                    balance = self.ctf_contract.functions.balanceOf(
                        self.account.address,
                        pos_id
                    ).call()
                    if balance > 0:
                        remaining_positions += 1
                        print(f"   ⚠️  Position {position['condition_id'][:10]}... still has {balance} shares")
                except:
                    pass

            if remaining_positions == 0:
                print(f"   ✅ All positions cleared from wallet!")
                # Individual trades already logged above, no need for aggregate
            else:
                print(f"   ⚠️  {remaining_positions} positions still remain (may need to wait for pending transactions)")

        # Get final balance for return
        try:
            final_balance = usdc_e_contract.functions.balanceOf(self.account.address).call() / 1e6
        except:
            final_balance = 0

        # Return dict with detailed stats
        return {
            'total': redeemed_count,
            'wins': wins_redeemed,
            'losses': losses_redeemed,
            'balance': final_balance
        }


def main():
    """Main entry point"""
    print("=" * 80)
    print("POLYMARKET WALLET CLEANER - REDEEMS ALL POSITIONS")
    print("⚠️  WARNING: This will redeem BOTH winners AND losers!")
    print("=" * 80)

    # Get private key
    private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
    if not private_key:
        print("❌ POLYMARKET_PRIVATE_KEY not set in .env file")
        return

    # Create redeemer and run
    redeemer = EnhancedWinningsRedeemer(private_key)
    count = redeemer.redeem_all()

    print("\n" + "=" * 80)
    if count > 0:
        print(f"🧹 Successfully redeemed {count} position(s) - wallet cleaned!")
        print(f"   (This includes both winners and losers)")
    else:
        print("💡 No resolved positions found to redeem")
    print("=" * 80)


if __name__ == "__main__":
    main()