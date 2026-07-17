"""
Streamlit File Uploader Component

Handles uploading findings files to the virtual filesystem.
Displays list of uploaded findings in the sidebar.
"""

import streamlit as st
from typing import List, Optional


def render_file_uploader(service) -> None:
    """
    Render file upload interface for findings.
    
    Args:
        service: PenTestAgentService instance
    """
    st.sidebar.markdown("### 📁 Upload Findings")
    
    uploaded_file = st.sidebar.file_uploader(
        "Upload findings file",
        type=["txt", "md", "json", "log", "csv", "xml"],
        help="Upload scan results, recon data, or other findings"
    )
    
    if uploaded_file is not None:
        filename = uploaded_file.name
        content = uploaded_file.read().decode('utf-8')
        
        with st.sidebar.spinner(f"Uploading {filename}..."):
            import asyncio
            success = asyncio.run(service.upload_finding(filename, content))
            
        if success:
            st.sidebar.success(f"✅ Uploaded: {filename}")
            # Clear the uploader to allow re-upload
            st.rerun()
        else:
            st.sidebar.error(f"❌ Failed to upload {filename}")
            

def render_findings_list(service) -> List[str]:
    """
    Render list of uploaded findings files in sidebar.
    
    Args:
        service: PenTestAgentService instance
        
    Returns:
        List of uploaded filenames
    """
    st.sidebar.markdown("### 📂 Uploaded Findings")
    
    import asyncio
    findings = asyncio.run(service.list_findings())
    
    if not findings:
        st.sidebar.info("No findings uploaded yet.")
        return []
    
    st.sidebar.write(f"**{len(findings)}** files available:")
    
    for filename in findings:
        st.sidebar.text(f"📄 {filename}")
        
    return findings


def display_findings_content(service, filename: str) -> Optional[str]:
    """
    Display content of a selected findings file.
    
    Args:
        service: PenTestAgentService instance
        filename: Name of file to display
        
    Returns:
        File content if found, None otherwise
    """
    import asyncio
    
    # Download from store
    content = asyncio.run(service.download_report(f"findings/{filename}"))
    
    if content:
        st.expander(f"📄 Preview: {filename}").code(content, language="text")
        return content
    
    return None
