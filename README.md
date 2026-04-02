# Job Application Tracking Script

## Overview
This script is designed to track job applications and help users organize key information for interview preparation. It aims to efficiently extract and store relevant details such as:

- Job description  
- Company name  
- HR contact emails  
- Work location  
- Salary range  

## Current Challenges

### 1. Information Extraction Accuracy
The current implementation relies on regular expressions, which often leads to inconsistent and incomplete data extraction.

To improve accuracy and robustness, we are considering:
- Integrating a lightweight LLM
- Using an external LLM API for structured information extraction

### 2. Data Input Efficiency
Currently, users must manually copy and paste job descriptions into the command line, which is not user-friendly.

Potential improvements include:
- Developing a graphical user interface (GUI)
- Browser integration (e.g., extension or bookmarklet)
- Automatic parsing from job posting URLs

## Future Improvements
- Improve extraction reliability using AI-based methods  
- Streamline user interaction and input workflow  
- Enhance data storage and retrieval for better tracking and analysis  
