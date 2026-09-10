"""Explicit opt-in paid Gemini test, using generated fictional documents only.
Run: .venv/bin/python -m tests.live_document_check
"""
import io
import json
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from rental_agent.env import load_dotenv
from rental_agent.services.document_checks import GeminiDocumentReader


def sample(name='Sample Driver', expiry='2035-01-01', blurred=False, file_format='PNG', kind='PASSPORT'):
    image=Image.new('RGB',(1400,1200),'white');draw=ImageDraw.Draw(image)
    try:font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',42)
    except OSError:font=ImageFont.load_default(size=42)
    lines=['FICTIONAL SAMPLE '+kind,'FOR SOFTWARE TESTING ONLY','Not valid identification',
           'Full name: '+name,'Date of birth: 1995-01-01','Date of expiry: '+expiry,
           'Date of issue: 2015-01-01','Issuing country: United Arab Emirates (AE)',
           'Document number: SAMPLE ONLY']
    for i,line in enumerate(lines):draw.text((65,70+i*110),line,fill='black',font=font)
    if blurred:image=image.filter(ImageFilter.GaussianBlur(22))
    out=io.BytesIO();image.save(out,format=file_format);return out.getvalue()


def main():
    load_dotenv();reader=GeminiDocumentReader();results=[]
    cases = [('clear',{}),('expired',{'expiry':'2020-01-01'}),
             ('different_name',{'name':'Other Sample Driver'}),('blurred',{'blurred':True}),
             ('clear_pdf', {'file_format':'PDF'}),
             ('emirates_id', {'kind':'EMIRATES ID'}),
             ('uae_licence', {'kind':'DRIVING LICENCE', 'file_format':'PDF'})]
    selected = os.getenv('DOCUMENT_TEST_CASE')
    if selected:
        cases = [(label, kwargs) for label, kwargs in cases if label == selected]
        if not cases:
            raise ValueError('Unknown DOCUMENT_TEST_CASE')
    for label,kwargs in cases:
        try:
            mime = 'application/pdf' if kwargs.get('file_format') == 'PDF' else 'image/png'
            value=reader.extract(sample(**kwargs),mime)
            expected_type={'emirates_id':'emirates_id','uae_licence':'driving_license'}.get(label,'passport')
            passed = ((value.readable and value.document_type==expected_type and value.full_name=='Sample Driver'
                       and value.expiry_date and value.expiry_date.isoformat()=='2035-01-01'
                       and value.date_of_birth and value.date_of_birth.isoformat()=='1995-01-01'
                       and value.issue_date and value.issue_date.isoformat()=='2015-01-01'
                       and value.issuing_country=='AE'
                       and value.confidence >= .9) if label in {'clear', 'clear_pdf','emirates_id','uae_licence'}
                else value.expiry_date.isoformat()=='2020-01-01' if label=='expired' and value.expiry_date
                else value.full_name=='Other Sample Driver' if label=='different_name'
                else not value.readable if label=='blurred' else False)
            results.append({'case':label,'passed':passed,'readable':value.readable,
                            'document_type':value.document_type,'confidence':value.confidence})
        except Exception as exc:
            message=str(getattr(exc,'message',''))[:1200]
            for key,value in os.environ.items():
                if len(value)>6 and any(word in key for word in ['KEY','TOKEN','SECRET']):
                    message=message.replace(value,'[redacted]')
            results.append({'case':label,'passed':False,'error':type(exc).__name__,
                            'code':getattr(exc,'code',None),'message':message})
            if getattr(exc,'code',None) in {400,401,403,404}: break
    print(json.dumps({'model':reader.model,'results':results},indent=2))
    return all(r['passed'] for r in results)


if __name__=='__main__':raise SystemExit(0 if main() else 1)
