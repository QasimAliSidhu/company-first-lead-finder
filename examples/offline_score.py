"""Fictional evidence passed through the actual detector and scorer."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.support_signal_detector import detect
from src.scorer import score
from src.config import load
pages=[dict(url='https://atlas-support.example/about',text='Managed IT SaaS software technical support support team 24/7 support remote support help center knowledge base support portal API documentation fully remote')]
signals,_=detect(pages,{})
total,priority,breakdown=score(signals,['careers@atlas-support.example'],load()['scoring'])
print(json.dumps(dict(company='Atlas Support (fictional)',score=total,priority=priority,contributions=breakdown,evidence=signals),indent=2))
