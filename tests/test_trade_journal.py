"""
Tests for trade_journal.py
"""

import pytest
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from trade_journal import TradeJournal, SignalStatus


class TestTradeJournal:
    """Test trade journal."""
    
    def test_log_signal(self):
        """Test logging a signal."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            signal_id = journal.log_signal(
                "BTC-USDT",
                {"recommended": "BUY", "signal_strength": 75},
                "Test signal"
            )
            
            assert signal_id is not None
            assert len(signal_id) > 0
            
            stats = journal.get_stats()
            assert stats["total"] == 1
            assert stats["pending"] == 1
        finally:
            os.unlink(db_path)
    
    def test_log_approval(self):
        """Test logging approval."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            signal_id = journal.log_signal(
                "BTC-USDT",
                {"recommended": "BUY"},
                "Test"
            )
            
            journal.log_approval(signal_id, True)
            
            stats = journal.get_stats()
            assert stats["approved"] == 1
            assert stats["pending"] == 0
        finally:
            os.unlink(db_path)
    
    def test_log_result(self):
        """Test logging outcome."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            signal_id = journal.log_signal(
                "BTC-USDT",
                {"recommended": "BUY"},
                "Test"
            )
            
            journal.log_result(signal_id, "Profit: 5%")
            
            # Result should be logged (no exception)
            assert True
        finally:
            os.unlink(db_path)
    
    def test_verify_chain_integrity(self):
        """Test hash chain integrity."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            # Log multiple signals
            for i in range(5):
                journal.log_signal(
                    f"COIN{i}",
                    {"recommended": "BUY"},
                    f"Test {i}"
                )
            
            # Verify chain
            assert journal.verify_chain_integrity() == True
        finally:
            os.unlink(db_path)
    
    def test_export_journal_report(self):
        """Test report export."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            journal.log_signal(
                "BTC-USDT",
                {"recommended": "BUY", "signal_strength": 80},
                "Test signal"
            )
            
            report = journal.export_journal_report(days=30)
            
            assert "Trade Journal Report" in report
            assert "BTC-USDT" in report
        finally:
            os.unlink(db_path)
    
    def test_chain_hash_linking(self):
        """Test that hashes are properly linked."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            journal = TradeJournal(db_path)
            
            # First signal
            id1 = journal.log_signal("COIN1", {"rec": "BUY"}, "Test 1")
            # Second signal
            id2 = journal.log_signal("COIN2", {"rec": "SELL"}, "Test 2")
            
            # Get signals from DB
            cursor = journal._conn.execute(
                "SELECT this_hash, prev_hash FROM signals ORDER BY timestamp"
            )
            rows = cursor.fetchall()
            
            # Second signal's prev_hash should match first signal's this_hash
            assert rows[1][1] == rows[0][0]
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
