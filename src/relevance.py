"""Reject unrelated search results before treating them as companies."""
import re
from urllib.parse import urlsplit
from .domain_utils import blocked

def relevant(result, category, location, settings):
    if blocked(result.get('url',''), settings['blocked_domains']):
        return False
    text = ' '.join([result.get('title',''),result.get('snippet',''),result.get('url','')]).lower()
    city = re.sub(r'\s+(?:[A-Z]{2}|USA|United States)$', '', location).strip().lower()
    if city not in text:
        return False
    pattern = settings.get('industry_patterns',{}).get(category)
    if not pattern:
        stopwords = {'company','companies','provider','providers','services','professional','professionals'}
        tokens = [word for word in re.findall(r'[a-z0-9]+',category.lower()) if word not in stopwords]
        pattern = '|'.join(r'\b'+re.escape(word)+r'\b' for word in tokens)
    if pattern and not re.search(pattern,text,re.I):
        return False
    path = urlsplit(result['url']).path.lower()
    if re.search(r'/(?:blog|news|articles|learn|think|wiki|login|sign-in|job-training-programs)(?:/|$)',path):
        return False
    if re.search(r'\b(?:top|best)\s+\d+|\bdirectory\b|\blist of\b',result.get('title',''),re.I):
        return False
    return True
