"""
Tests for task_queue.py
"""

import pytest
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import path_setup  # registers all sub-packages

from task_queue import TaskQueue, TaskStatus


class TestTaskQueue:
    """Test task queue."""
    
    def test_enqueue(self):
        """Test task enqueue."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            task_id = queue.enqueue(
                "test_task",
                {"data": "value"},
                scheduled_for=time.time()
            )
            
            assert task_id is not None
            assert len(task_id) > 0
            
            stats = queue.get_stats()
            assert stats["queued"] == 1
        finally:
            os.unlink(db_path)
    
    def test_dequeue_ready(self):
        """Test dequeuing ready tasks."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            # Enqueue task scheduled for now
            task_id = queue.enqueue(
                "test_task",
                {"data": "value"},
                scheduled_for=time.time()
            )
            
            # Dequeue ready tasks
            tasks = queue.dequeue_ready()
            
            assert len(tasks) == 1
            assert tasks[0]["task_type"] == "test_task"
            
            # Task should now be marked as running
            stats = queue.get_stats()
            assert stats["running"] == 1
        finally:
            os.unlink(db_path)
    
    def test_mark_complete(self):
        """Test marking task complete."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            task_id = queue.enqueue("test", {}, scheduled_for=time.time())
            queue.dequeue_ready()
            queue.mark_complete(task_id)
            
            stats = queue.get_stats()
            assert stats["complete"] == 1
            assert stats["running"] == 0
        finally:
            os.unlink(db_path)
    
    def test_mark_failed(self):
        """Test marking task failed."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            task_id = queue.enqueue("test", {}, scheduled_for=time.time())
            queue.dequeue_ready()
            queue.mark_failed(task_id, retry=False)
            
            stats = queue.get_stats()
            assert stats["failed"] == 1
        finally:
            os.unlink(db_path)
    
    def test_reset_stale_running(self):
        """Test resetting stale running tasks."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            # Manually insert a stale running task
            queue._conn.execute("""
                INSERT INTO tasks (task_id, task_type, payload, scheduled_for, 
                                 priority, status, created_at, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, ("stale_task", "test", "{}", time.time(), 5, "running", 
                  time.time() - 400, time.time() - 400))
            queue._conn.commit()
            
            # Reset stale tasks
            reset_count = queue.reset_stale_running(stale_seconds=300)
            
            assert reset_count == 1
            
            # Task should be queued again
            stats = queue.get_stats()
            assert stats["queued"] == 1
        finally:
            os.unlink(db_path)
    
    def test_future_task_not_ready(self):
        """Test that future tasks are not dequeued."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            queue = TaskQueue(db_path)
            
            # Enqueue task for future
            queue.enqueue(
                "future_task",
                {},
                scheduled_for=time.time() + 3600  # 1 hour from now
            )
            
            # Should not be ready
            tasks = queue.dequeue_ready()
            assert len(tasks) == 0
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
