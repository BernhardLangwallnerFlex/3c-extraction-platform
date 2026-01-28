from ocr.ocr_agentic import AgenticPDFOCRExtractor
from ocr.ocr_docling import DoclingPDFOCRExtractor
import os
from openai import OpenAI
import time

openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

agentic_ocr_engine = AgenticPDFOCRExtractor()
docling_ocr_engine = DoclingPDFOCRExtractor()

i = 0
for file in os.listdir("3C_testdaten_pdf"):
    print("Processing file: ", f"3C_testdaten_pdf/{file}")
    if file.endswith(".pdf"):
        # time the extraction
        start_time = time.time()
        agentic_ocr_result, _ = agentic_ocr_engine.extract_text(f"3C_testdaten_pdf/{file}")
        agentic_ocr_time = time.time() - start_time
        start_time = time.time()
        docling_ocr_result, _ = docling_ocr_engine.extract_text(f"3C_testdaten_pdf/{file}")
        docling_ocr_time = time.time() - start_time
        print(f"Time taken: {agentic_ocr_time} seconds for Agentic OCR and {docling_ocr_time} seconds for Docling OCR")
        with open(f"3C_testdaten_md/{file.replace('.pdf', '_agentic_ocr.md')}", "w") as f:
            f.write(agentic_ocr_result)
        with open(f"3C_testdaten_md/{file.replace('.pdf', '_docling_ocr.md')}", "w") as f:
            f.write(docling_ocr_result)
        ## compare the two results using chatgpt and save the result to a json file
        #prompt = f"Compare the following two markdown results and return a json object with the differences: #{agentic_ocr_result} and {docling_ocr_result}"
       # response = openai.chat.completions.create(
       #     model="gpt-4",
       #     messages=[{"role": "user", "content": prompt}],
       # )
       # print(response.choices[0].message.content)
    i+=1
    if i > 3:
        break