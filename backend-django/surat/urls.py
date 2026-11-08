# apps/surat/urls.py
from django.urls import path
from .views import (
    AutoSuratChatAPIView,
    
    SuratTypeListAPIView,
    SuratRequestDetailAPIView,
    SuratRequestListAPIView,
    LecturerApprovalActionAPIView,
    SuratVerifyAPIView,
    
    ChatThreadCreateAPIView
)

urlpatterns = [
    path('chat/', AutoSuratChatAPIView.as_view(), name='auto-surat-chat'),
    
    path('types/', SuratTypeListAPIView.as_view(), name='surat-type-list'),
    path('requests/', SuratRequestListAPIView.as_view(), name='surat-request-list'),
    path('requests/<int:pk>/', SuratRequestDetailAPIView.as_view(), name='surat-request-detail'),
    path('approval/<int:approval_id>/action/', LecturerApprovalActionAPIView.as_view(), name='surat-approval-action'),
    path('verify/<str:token>/', SuratVerifyAPIView.as_view(), name='surat-verify'), 
    # path('request/', SuratRequestCreateAPIView.as_view(), name='surat-request-create'),
    # path('requests/<int:pk>/re-draft/', SuratRequestRedraftAPIView.as_view(), name='surat-request-redraft'),
    # path('chat/init/', ChatThreadCreateAPIView.as_view(), name='chat-init'),
    # path('chat/<str:thread_id>/', ChatSessionMessageAPIView.as_view(), name='chat-session')
]