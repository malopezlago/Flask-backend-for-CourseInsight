import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from functools import wraps

# Import services
from kt_service import trace_student_knowledge
from llm_service import (
    generate_solution_steps_for_question,
    classify_bloom_level_for_question,
    generate_embedding_for_text,
    extract_taxonomy_tags_for_text,
    generate_feedback_with_graphrag_llm,
    summarize_content,
    update_graph_with_new_data
)

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Basic logging configuration for Flask app
if not app.debug: # Avoid duplicate handlers if Flask's debug reloader is active
    import logging
    logging.basicConfig(level=logging.INFO)


# --- Configuration ---
LMS_API_KEY = os.getenv("LMS_PLUGIN_API_KEY")

if not LMS_API_KEY:
    app.logger.error("LMS_PLUGIN_API_KEY not found in environment variables.")
    raise ValueError("LMS_PLUGIN_API_KEY not found in environment variables.")

# --- API Key Authentication Decorator ---
def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            app.logger.warning("Missing Authorization Header")
            return jsonify({"status": "error", "message": "Missing Authorization Header"}), 401
        
        if not LMS_API_KEY: 
            app.logger.error("API Key is not configured on the server (LMS_PLUGIN_API_KEY missing).")
            return jsonify({"status": "error", "message": "API Key not configured on server"}), 500
        try:
            auth_type, api_key_received = auth_header.split()
            if auth_type.lower() != 'bearer' or api_key_received != LMS_API_KEY:
                app.logger.warning(f"Invalid API Key. Received type: {auth_type}")
                return jsonify({"status": "error", "message": "Invalid API Key"}), 401
        except ValueError:
            app.logger.warning(f"Invalid Authorization Header format. Header: {auth_header}")
            return jsonify({"status": "error", "message": "Invalid Authorization Header format"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- API Endpoints ---

@app.route('/api/generate_feedback', methods=['POST'])
@require_api_key
def generate_feedback():
    data = request.json
    app.logger.info(f"Received data for /api/generate_feedback: {data}")

    try:
        attempt_id = data.get('attemptid')
        user_id = data.get('userid')
        course_id = data.get('courseid')
        # quiz_id = data.get('quizid') # Available if needed
        responses = data.get('responses', []) # Student's answers in the current attempt
        history = data.get('history', []) # Broader interaction history
        content_views = data.get('contentviews', []) # Log data of content views

        if not all([attempt_id, user_id, course_id]):
            return jsonify({"status": "error", "message": "Missing required fields: attemptid, userid, or courseid"}), 400

        # 1. Perform Knowledge Tracing
        student_kt_state = trace_student_knowledge(user_id, course_id, responses, history, content_views)

        # 2. Prepare context for GraphRAG/LLM (can be expanded)
        #    For now, we'll pass the attempt data and KT state.
        #    You might also fetch relevant course learning objectives or content metadata here.
        course_context_for_llm = {"course_id": course_id} # Add more course-specific details if needed

        # 3. Generate feedback using GraphRAG and LLM
        feedback_text = generate_feedback_with_graphrag_llm(data, student_kt_state, course_context_for_llm)
        
        return jsonify({
            "status": "success",
            "feedback": feedback_text
        })
    except Exception as e:
        app.logger.error(f"Error in /api/generate_feedback: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred while generating feedback."}), 500

@app.route('/api/analyze_question', methods=['POST'])
@require_api_key
def analyze_question():
    data = request.json
    app.logger.info(f"Received data for /api/analyze_question: {data}")

    try:
        question_data = data.get('question', {})
        question_id = question_data.get('id')
        question_text = question_data.get('text')
        question_type = question_data.get('type')
        answers = question_data.get('answers') # e.g., list of choices for multichoice

        if not all([question_id, question_text, question_type]):
             return jsonify({"status": "error", "message": "Missing required question fields: id, text, or type"}), 400

        # 1. Generate solution steps
        solution_steps = generate_solution_steps_for_question(question_text, question_type, answers)

        # 2. Classify Bloom's Taxonomy level
        cognitive_level = classify_bloom_level_for_question(question_text, answers)

        # 3. Generate semantic embedding
        embedding = generate_embedding_for_text(question_text)
        
        # 4. Extract taxonomy tags (keywords, concepts)
        taxonomy_tags = extract_taxonomy_tags_for_text(question_text)

        return jsonify({
            "status": "success",
            "question_id": question_id, # Echo back the question ID for mapping
            "embedding": embedding,
            "cognitive_level": cognitive_level, # e.g., "Applying" or an integer code
            "solution_steps": solution_steps,   # List of strings
            "taxonomy": taxonomy_tags           # List of dicts: [{"name": "Skill", "type": "skill"}, ...]
        })
    except Exception as e:
        app.logger.error(f"Error in /api/analyze_question: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred while analyzing the question."}), 500

@app.route('/api/analyze_course_content', methods=['POST'])
@require_api_key
def analyze_course_content():
    data = request.json
    app.logger.info(f"Received data for /api/analyze_course_content: {data}")

    try:
        # course_id = data.get('courseid') # Available if needed for context
        books_data = data.get('books', [])
        h5p_transcripts_data = data.get('h5p_transcripts', [])

        analyzed_book_chapters = []
        for book in books_data:
            for chapter in book.get('chapters', []):
                chapter_id = chapter.get('chapterid')
                content = chapter.get('content')
                if not all([chapter_id, content]):
                    app.logger.warning(f"Skipping chapter due to missing ID or content: {chapter}")
                    continue
                
                summary = summarize_content(content)
                tags = extract_taxonomy_tags_for_text(content)
                embedding = generate_embedding_for_text(content) # Also generate embedding for content
                analyzed_book_chapters.append({
                    "chapterid": chapter_id,
                    "summary": summary,
                    "taxonomy": tags,
                    "embedding": embedding 
                })

        analyzed_h5p_activities = []
        for h5p in h5p_transcripts_data:
            h5p_id = h5p.get('h5pid')
            transcript = h5p.get('transcript')
            if not all([h5p_id, transcript]):
                app.logger.warning(f"Skipping H5P due to missing ID or transcript: {h5p}")
                continue

            summary = summarize_content(transcript)
            tags = extract_taxonomy_tags_for_text(transcript)
            embedding = generate_embedding_for_text(transcript) # Also generate embedding for content
            analyzed_h5p_activities.append({
                "h5pid": h5p_id,
                "summary": summary,
                "taxonomy": tags,
                "embedding": embedding
            })
            
        return jsonify({
            "status": "success",
            "bookchapters": analyzed_book_chapters,
            "h5pactivities": analyzed_h5p_activities
        })
    except Exception as e:
        app.logger.error(f"Error in /api/analyze_course_content: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred while analyzing course content."}), 500

@app.route('/api/update_knowledge_graph', methods=['POST'])
@require_api_key
def update_knowledge_graph():
    data = request.json # Expects data from analyze_course_content and analyze_question
    app.logger.info(f"Received data for /api/update_knowledge_graph: {data}")

    try:
        course_id = data.get('courseid')
        # The Moodle plugin currently sends analyzed content directly.
        # If you change Moodle to send raw content and want this endpoint to trigger analysis first,
        # you'd call analyze_course_content and analyze_question logic here.
        # For now, assume 'data' contains pre-analyzed metadata.
        
        # This endpoint might receive the output of `analyze_course_content` and potentially
        # a batch of `analyze_question` outputs if you decide to batch question analysis before updating the graph.
        # For simplicity, let's assume the Moodle plugin will call analyze_course_content,
        # then analyze_question (perhaps for all new/updated questions),
        # and then call this endpoint with the *results* of those analyses.
        # Or, this endpoint could be more of a trigger, and GraphRAG pulls data from a DB
        # where the analysis results are stored.

        # Let's assume `data` might contain keys like `analyzed_book_chapters`, `analyzed_h5p_activities`,
        # and `analyzed_questions` (a list of outputs from the analyze_question endpoint).
        
        # For now, the Moodle plugin calls this with just course_id after content analysis.
        # We'll use a simplified call to the placeholder.
        # In a real scenario, you'd pass the actual analyzed data.
        
        # Placeholder: In a real system, you'd fetch/pass the actual analyzed data.
        # For now, we'll pass empty structures or rely on GraphRAG to fetch if designed that way.
        mock_analyzed_content = {
            "bookchapters": data.get("bookchapters", []), # If Moodle sends it
            "h5pactivities": data.get("h5pactivities", []) # If Moodle sends it
        }
        mock_analyzed_questions = data.get("questions_metadata", []) # If Moodle sends it

        message = update_graph_with_new_data(course_id, mock_analyzed_content, mock_analyzed_questions)
        
        app.logger.info(message)
        return jsonify({
            "status": "success",
            "message": message
        })
    except Exception as e:
        app.logger.error(f"Error in /api/update_knowledge_graph: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "An internal error occurred while updating the knowledge graph."}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)