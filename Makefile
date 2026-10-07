.PHONY: all test demo clean

all: test

test:
	python -m pytest -v

demo:
	python src/datagen.py
	python src/classify.py
	python src/policy.py
	python src/simulate.py
	streamlit run app/main.py

clean:
	python -c "import shutil, os, glob; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob('**/__pycache__', recursive=True)]; shutil.rmtree('.pytest_cache', ignore_errors=True)"
