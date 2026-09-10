"""Durable WhatsApp worker and scheduled retention/hold maintenance.

Run beside run_webhook:app. Use PostgreSQL in production. A dead job blocks its
customer lane and /ready reports failure until an operator retries it.
"""
import argparse
import logging
import signal
import time

from sqlalchemy import select

from rental_agent.whatsapp.jobs import enqueue, retry_dead, utcnow
from rental_agent.store.models import WorkItem, WorkerHeartbeat


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--failed',action='store_true')
    parser.add_argument('--retry',type=int)
    args=parser.parse_args()
    from run_webhook import app, session_factory
    if not app.state.durable: raise SystemExit('Set WHATSAPP_DURABLE=1 for the worker')
    if args.failed or args.retry:
        with session_factory() as db:
            if args.retry:
                retry_dead(db,args.retry);db.commit()
            else:
                for job in db.scalars(select(WorkItem).where(WorkItem.status=='dead')):
                    print(f'{job.id} {job.kind} attempts={job.attempts} error={job.last_error}')
        return
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    last_tick=0
    while not stopped:
        try:
            if time.monotonic()-last_tick>=30:
                now=utcnow()
                with session_factory() as db:
                    db.merge(WorkerHeartbeat(name='whatsapp',seen_at=now))
                    enqueue(db,key='maintenance:'+now.strftime('%Y%m%d%H%M'),lane='maintenance',
                            kind='maintenance',payload={})
                    db.commit()
                last_tick=time.monotonic()
            worked=app.state.worker.run_once()
            if args.once: break
            if not worked:time.sleep(.5)
        except Exception as exc:
            logging.error('Worker operation failed: %s',type(exc).__name__)
            if args.once:raise
            time.sleep(2)


if __name__=='__main__':main()
