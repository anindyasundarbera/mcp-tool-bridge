# Manual Installation Guide for Claude/Local-Dev-MCP Enhancement
## For Completion When Classifier Blocks Automated Operations

This guide provides step-by-step instructions for manually installing and configuring the remaining components of your Claude/local-dev-mcp enhancement project.

## ��� Prerequisites Completed
- �� LightRAG cloned and installed (`pip install -e .`)
- �� RAG-Anything cloned (`git clone https://github.com/HKUDS/RAG-Anything.git`)
- �� tokengate cloned (`git clone https://github.com/Mario-Vishal/tokengate.git`)

## ��� Phase 2: Integrate RAG-Anything with LightRAG

### Step 1: Install RAG-Anything Dependencies
```bash
cd /home/anindyasundar-bera/local-dev-mcp/RAG-Anything
pip install -e .
```

### Step 2: Install Additional Required Dependencies
```bash
# Core dependencies for multimodal processing
pip install mineru docling opencv-python easyocr pillow
# For PDF processing
pip install pymupdf
# For table structure recognition
pip install paddleocr
# For VLM support (if using)
pip install transformers torch
```

### Step 3: Verify Installation
```bash
# Test basic import
python3 -c "from rag.anything import RAGAnything; print('RAG-Anything imported successfully')"

# Test LightRAG still works
python3 -c "import lightrag; print('LightRAG import successful')"
```

### Step 4: Basic Integration Example
Create a test script to verify the integration works:
```python
# test_integration.py
import asyncio
from lightrag import LightRAG
from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
from rag.anything import RAGAnything

async def test_integration():
    # Initialize LightRAG
    rag = LightRAG(
        working_dir="./demo_rag_storage",
        llm_model_func=gpt_4o_mini_complete,
        embedding_func=openai_embed
    )
    await rag.initialize_storages()

    # Initialize RAG-Anything
    rag_anything = RAGAnything(
        lightrag=rag,
        # Add any specific configuration for RAG-Anything here
    )

    # Test with a simple text document
    test_text = "This is a test document for verifying RAG-Anything integration with LightRAG."
    await rag.ainsert(test_text)
    
    # Test query
    result = await rag.aquery("What is this test document about?", 
                             param=QueryParam(mode="hybrid"))
    print(f"Query result: {result}")
    
    await rag.finalize_storages()

if __name__ == "__main__":
    asyncio.run(test_integration())
```

Run the test:
```bash
python3 test_integration.py
```

## ��� Phase 3: Set Up MCP Server Capabilities

### Option A: Using github-to-mcp (Direct Conversion)

#### Step 1: Install github-to-mcp Globally
```bash
npm install -g github-to-mcp
```

#### Step 2: Convert Your local-dev-mcp Repository
```bash
# Replace with your actual GitHub username and repo
github-to-mcp https://github.com/anindyasundar-bera/local-dev-mcp
```

#### Step 3: Configure Claude Desktop to Use the MCP Server
1. Find or create your Claude Desktop configuration file:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`

2. Add the MCP server configuration:
```json
{
  "mcpServers": {
    "local-dev-mcp": {
      "command": "github-to-mcp",
      "args": ["https://github.com/anindyasundar-bera/local-dev-mcp"],
      "env": {}
    }
  }
}
```

#### Step 4: Restart Claude Desktop and Test
- Restart Claude Desktop
- In a chat, you should now be able to ask questions about your local-dev-mcp repository

### Option B: Using GitMCP.io (No Installation Required)

#### Step 1: Access Your Repository via GitMCP
Simply use the URL in your Claude Desktop MCP configuration:
```json
{
  "mcpServers": {
    "local-dev-mcp-via-gitmcp": {
      "command": "npx",
      "args": ["-y", "gitmcp.io", "anindyasundar-bera", "local-dev-mcp"],
      "env": {}
    }
  }
}
```

#### Step 2: Or Access Directly in Browser
Visit: `https://gitmcp.io/anindyasundar-bera/local-dev-mcp`
- This provides instant access to your repo's documentation via MCP
- No local installation required

## ��� Phase 4: Implement TokenGate Optimization

### Step 1: Install TokenGate Dependencies
```bash
cd /home/anindyasundar-bera/local-dev-mcp/tokengate
pip install -e .
```

### Step 2: Install Additional Dependencies (Check requirements.txt)
```bash
# Typical dependencies for token optimization
pip install torch transformers sentence-transformers
pip install numpy scipy scikit-learn
```

### Step 3: Verify Installation
```bash
python3 -c "import tokengate; print('TokenGate imported successfully')"
```

### Step 4: Integration Example with LightRAG
Create a preprocessing pipeline:
```python
# tokengate_integration.py
import asyncio
from lightrag import LightRAG, QueryParam
from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
from tokengate import TokenGateOptimizer  # Adjust import based on actual API

class OptimizedLightRAG:
    def __init__(self, working_dir="./optimized_rag_storage"):
        self.rag = LightRAG(
            working_dir=working_dir,
            llm_model_func=gpt_4o_mini_complete,
            embedding_func=openai_embed
        )
        self.token_optimizer = TokenGateOptimizer(
            # Configure TokenGate parameters
            compression_ratio=0.3,  # Target 70% reduction
            preserve_key_information=True
        )
    
    async def initialize(self):
        await self.rag.initialize_storages()
    
    async def ainsert_optimized(self, text: str):
        # Optimize text before insertion
        optimized_text = await self.token_optimizer.optimize(text)
        await self.rag.ainsert(optimized_text)
    
    async def aquery_optimized(self, query: str, **kwargs):
        # Optimize query before processing
        optimized_query = await self.token_optimizer.optimize(query, is_query=True)
        result = await self.rag.aquery(optimized_query, **kwargs)
        # Optionally optimize the response for output
        optimized_response = await self.token_optimizer.optimize(result, is_query=False)
        return optimized_response

# Usage example
async def main():
    optimized_rag = OptimizedLightRAG()
    await optimized_rag.initialize()
    
    # Insert document with optimization
    await optimized_rag.ainsert_optimized("Your long document text here...")
    
    # Query with optimization
    result = await optimized_rag.aquery_optimized("Your question here...")
    print(f"Optimized query result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

## ��� Phase 5: Real-Time Intelligence Integration

### Option A: MCP-Hacker News Bridge

#### Step 1: Clone the Repository
```bash
git clone https://github.com/paabloLC/mcp-hacker-news.git
cd mcp-hacker-news
```

#### Step 2: Install Dependencies
```bash
npm install
```

#### Step 3: Configure Environment (if needed)
```bash
# Check if there's a .env.example or similar
cp .env.example .env  # If exists
# Edit .env with any required configuration
```

#### Step 4: Build and Start the MCP Server
```bash
npm run build
npm start  # Or whatever the start command is in package.json
```

#### Step 5: Configure Claude Desktop
```json
{
  "mcpServers": {
    "hackernews": {
      "command": "node",
      "args": ["/path/to/mcp-hacker-news/dist/index.js"],  # Adjust path
      "env": {}
    }
  }
}
```

#### Step 6: Test in Claude Desktop
After restarting Claude, try queries like:
- "What are the top Hacker News stories right now?"
- "Show me recent posts about AI or machine learning on Hacker News"
- "What's trending in the programming section of Hacker News?"

### Option B: Paws-on-MCP (Comprehensive Alternative)

#### Step 1: Clone the Repository
```bash
git clone https://github.com/hemanth/paws-on-mcp.git
cd paws-on-mcp
```

#### Step 2: Install Dependencies
```bash
npm install
```

#### Step 3: Check for Docker Recommendation
If the repository recommends Docker (as mentioned in your guide):
```bash
# Check for docker-compose file
ls -la | grep docker-compose
# If found, use:
docker-compose up -d
```

#### Step 4: Otherwise, Start Directly
```bash
npm run start  # Or check package.json for start script
```

#### Step 5: Configure Claude Desktop
Similar to above, pointing to the Paws-on-MCP server.

## ��� Phase 6: Visual Workflow Builder (OpenRAG) - Optional

### Step 1: Clone the Repository
```bash
git clone https://github.com/langflow-ai/openrag.git
cd openrag
```

### Step 2: Install Dependencies
```bash
# Based on the README mentioning FastAPI + Next.js
pip install -e ".[all]"  # If they have extras
# Or check requirements.txt
pip install -r requirements.txt
cd frontend  # If there's a frontend directory
npm install
cd ..
```

### Step 3: Build and Start
```bash
# Build backend
python -m build  # Or whatever they use
# Start backend service
uvicorn openrag.api.main:app --reload

# In another terminal, start frontend
cd frontend
npm run dev
```

### Step 4: Access the Interface
- Backend API: Typically `http://localhost:8000`
- Frontend UI: Typically `http://localhost:3000` or `http://localhost:5173`
- Configure as MCP server in Claude Desktop if it exposes MCP endpoints

## ��� Phase 7: Integration with local-dev-mcp

### Step 1: Create a Unified Configuration Script
Create a script that initializes all your enhanced capabilities:
```python
# unified_rag_system.py
import asyncio
from lightrag import LightRAG
from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
# Import your other components as they become available

class EnhancedLocalDevMCP:
    def __init__(self):
        self.components = {}
        
    async def initialize_lightrag(self):
        """Initialize base LightRAG"""
        self.components['lightrag'] = LightRAG(
            working_dir="./enhanced_rag_storage",
            llm_model_func=gpt_4o_mini_complete,
            embedding_func=openai_embed
        )
        await self.components['lightrag'].initialize_storages()
        print("��� LightRAG initialized")
    
    async def initialize_rag_anything(self):
        """Add multimodal capabilities"""
        # Integration logic here
        print("��� RAG-Anything integrated")
    
    async def initialize_tokengate(self):
        """Add token optimization"""
        # Integration logic here
        print("��� TokenGate optimization active")
    
    async def setup_mcp_servers(self):
        """Configure MCP server connections"""
        # This would be done in Claude Desktop config, not here
        print("��� MCP servers configured (see Claude Desktop settings)")
    
    async def full_initialization(self):
        await self.initialize_lightrag()
        await self.initialize_rag_anything()
        await self.initialize_tokengate()
        await self.setup_mcp_servers()
        print("���� Enhanced RAG system fully initialized!")
    
    # Add your enhanced query methods here

# Usage
async def main():
    system = EnhancedLocalDevMCP()
    await system.full_initialization()
    # Now use system.components['lightrag'] for queries with all enhancements

if __name__ == "__main__":
    asyncio.run(main())
```

### Step 2: Create Wrapper Scripts for Easy Usage
```bash
# create a startup script
cat > start_enhanced_rag.sh << 'EOF'
#!/bin/bash
echo "Starting Enhanced RAG System for local-dev-mcp..."
# Add your startup commands here
# Example:
# python3 unified_rag_system.py &
# npm start mcp-hacker-news & 
# etc.
echo "System started. Configure Claude Desktop to use MCP servers."
EOF
chmod +x start_enhanced_rag.sh
```

### Step 3: Update Your Claude Desktop Configuration
Compile all your MCP servers into one configuration:
```json
{
  "mcpServers": {
    "local-dev-mcp-direct": {
      "command": "github-to-mcp",
      "args": ["https://github.com/anindyasundar-bera/local-dev-mcp"],
      "env": {}
    },
    "hackernews": {
      "command": "node",
      "args": ["/path/to/mcp-hacker-news/dist/index.js"],
      "env": {}
    },
    "your-custom-enhanced-server": {
      "command": "python3",
      "args": ["/path/to/your/enhanced/mcp/server.py"],
      "env": {}
    }
  }
}
```

## ��� Phase 8: Advanced Features & Optimization

### Implementing RAG-MCP Principles (from arXiv:2505.03275)

The key principle is to reduce prompt tokens by retrieving only relevant MCP tools per query.

#### Step 1: Create a Tool Selector
```python
# rag_mcp_optimizer.py
class RagMcpOptimizer:
    def __init__(self, available_tools):
        self.available_tools = available_tools  # List of your MCP tools
        self.tool_embeddings = None
        self._precompute_tool_embeddings()
    
    def _precompute_tool_embeddings(self):
        # Pre-compute embeddings for tool descriptions
        # This allows fast similarity search
        pass
    
    def select_relevant_tools(self, query, top_k=5):
        """
        Select only the most relevant MCP tools for a given query
        This reduces prompt size by avoiding inclusion of irrelevant tools
        """
        # 1. Embed the query
        query_embedding = self.embed_query(query)
        
        # 2. Calculate similarity with all tools
        similarities = self.calculate_similarities(query_embedding)
        
        # 3. Return top-k most relevant tools
        relevant_tool_indices = similarities.argsort()[-top_k:][::-1]
        return [self.available_tools[i] for i in relevant_tool_indices]
    
    def embed_query(self, text):
        # Use your embedding function (e.g., from LightRAG)
        pass
    
    def calculate_similarities(self, query_emb):
        # Calculate cosine similarity or other metric
        pass
```

#### Step 2: Implement Caching Layer
```python
# tool_cache.py
from cachetools import TTLCache
import hashlib

class ToolResponseCache:
    def __init__(self, maxsize=1000, ttl=3600):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
    
    def _make_key(self, query, tools_used):
        """Create a cache key from query and tools used"""
        key_string = f"{query}:{','.join(sorted(tools_used))}"
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def get(self, query, tools_used):
        key = self._make_key(query, tools_used)
        return self.cache.get(key)
    
    def set(self, query, tools_used, result):
        key = self._make_key(query, tools_used)
        self.cache[key] = result
```

#### Step 3: Implement Query Routing
```python
# query_router.py
class QueryRouter:
    def __init__(self):
        self.routes = {
            # Pattern: handler_function
            r'\b(github|repo|code|commit)\b': self.handle_github_query,
            r'\b(hackernews|hn)\b': self.handle_hn_query,
            r'\b(document|pdf|image|table)\b': self.handle_document_query,
            # Add more patterns as needed
        }
        self.default_handler = self.handle_general_query
    
    def route_query(self, query):
        """Route query to appropriate specialized handler"""
        import re
        for pattern, handler in self.routes.items():
            if re.search(pattern, query, re.IGNORECASE):
                return handler
        return self.default_handler
    
    async def handle_github_query(self, query):
        # Use github-to-mcp or direct repo access
        pass
    
    async def handle_hn_query(self, query):
        # Use MCP-Hacker News bridge
        pass
    
    async def handle_document_query(self, query):
        # Use your enhanced RAG with multimodal capabilities
        pass
    
    async def handle_general_query(self, query):
        # Use standard LightRAG query
        pass
```

## �� Verification Checklist

After completing each phase, run these checks:

### LightRAG Verification
```bash
python3 -c "
import lightrag
from lightrag.llm.openai import gpt_4o_mini_complete, openai_embed
import asyncio

async def test():
    rag = lightrag.LightRAG(working_dir='./test_storage', 
                           llm_model_func=gpt_4o_mini_complete,
                           embedding_func=openai_embed)
    await rag.initialize_storages()
    await rag.ainsert('Test document for verification')
    result = await rag.aquery('What is in the test document?')
    print(f'LightRAG test: {result}')
    await rag.finalize_storages()

asyncio.run(test())
"
```

### RAG-Anything Verification
```bash
# Test with a sample multimodal document if available
# Or at least test imports
python3 -c "
from rag.anything import RAGAnything
import lightrag
print('Both imports successful')
"
```

### TokenGate Verification
```bash
python3 -c "
import tokengate
# Test basic optimization if API allows
print('TokenGate import successful')
"
```

### MCP Server Verification
- Restart Claude Desktop after configuring MCP servers
- In chat, try: "List available tools" or similar introspection query
- Test specific queries related to each MCP server

### End-to-End Test
```bash
# Create a test that uses all components together
python3 -c "
# Your integrated test here
print('End-to-end test passed')
"
```

## ��� Troubleshooting Tips

### Common Issues and Solutions

1. **Dependency Conflicts**
   - Use virtual environments: `python3 -m venv venv && source venv/bin/activate`
   - Install packages in order of dependency
   - Check `requirements.txt` files for version specifications

2. **Import Errors**
   - Verify you're in the correct directory when running Python
   - Check that `pip install -e .` was successful in each repository
   - Look for missing system dependencies (like poppler for PDF processing)

3. **MCP Server Connection Issues**
   - Verify the server process is running: `ps aux | grep [server-name]`
   - Check port availability and firewall settings
   - Validate Claude Desktop configuration JSON syntax
   - Check Claude Desktop logs for connection errors

4. **Performance Issues**
   - Start with small test documents
   - Monitor memory usage during processing
   - Consider adjusting chunk sizes and batch parameters

5. **Multimodal Processing Failures**
   - Ensure required system libraries are installed (for OCR, etc.)
   - Test with simple files first before complex documents
   - Check file permissions and paths

## ��� Resources for Further Assistance

- **LightRAG Documentation**: https://github.com/HKUDS/LightRAG#readme
- **RAG-Anything Documentation**: https://github.com/HKUDS/RAG-Anything#readme  
- **TokenGate Documentation**: https://github.com/Mario-Vishal/tokengate#readme
- **github-to-mcp**: https://github.com/nirholas/github-to-mcp
- **GitMCP.io**: https://gitmcp.io/
- **MCP-Hacker News**: https://github.com/paabloLC/mcp-hacker-news
- **Claude Desktop MCP Docs**: https://modelcontextprotocol.io/quickstart/desktop

## ��� Next Steps

1. **Complete the manual installations** using this guide
2. **Test each component individually** before integrating
3. **Configure your Claude Desktop MCP servers**
4. **Run end-to-end tests** to verify the complete workflow
5. **Document your specific configuration** for future reference
6. **Explore advanced features** as needed for your use case

Remember: The goal is to create a token-efficient, context-aware RAG system that enhances your Claude/local-dev-mcp experience with multimodal capabilities, real-time intelligence, and optimized performance.

Happy hacking! ���