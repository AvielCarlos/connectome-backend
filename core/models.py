"""
Connectome Pydantic Models
Request/response schemas for all API surfaces.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from uuid import UUID

# ---------------------------------------------------------------------------
# Domain System
# ---------------------------------------------------------------------------

DomainType = Literal["iVive", "Eviva", "Aventi"]


# ---------------------------------------------------------------------------
# Auth / Users
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    display_name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


class UserProfile(BaseModel):
    id: UUID
    email: Optional[str]
    is_admin: bool = False
    subscription_tier: str
    fulfilment_score: float
    profile: Dict[str, Any]
    created_at: datetime
    last_active: Optional[datetime]
    domain: Optional[DomainType] = None  # active domain focus


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    interests: Optional[List[str]] = None
    goals_text: Optional[str] = None
    location: Optional[str] = None  # City/location for WorldAgent local events
    value_weights: Optional[Dict[str, int]] = None  # top-level values, 1-10 compass for Aura
    now_vector_prompt: Optional[str] = None  # user-editable Now vector instruction
    later_vector_prompt: Optional[str] = None  # user-editable Later/Future vector instruction
    travel_mode_enabled: Optional[bool] = None  # paid-tier opt-in to include non-local/travel opportunities


# ---------------------------------------------------------------------------
# Screen Specs (Server-Driven UI)
# ---------------------------------------------------------------------------

class ScreenAction(BaseModel):
    type: str  # "next_screen", "affiliate_link", "goal_update", "open_url"
    context: Optional[str] = None
    url: Optional[str] = None
    tracking_id: Optional[str] = None
    goal_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class ScreenComponent(BaseModel):
    type: str  # "hero_image", "headline", "body_text", "action_button", "progress_bar", etc.
    # Common fields — renderer skips unknown types gracefully
    text: Optional[str] = None
    source: Optional[str] = None
    alt: Optional[str] = None
    style: Optional[str] = None
    label: Optional[str] = None
    action: Optional[ScreenAction] = None
    value: Optional[float] = None  # for progress_bar
    color: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"  # forward-compatibility: unknown fields pass through


class FeedbackOverlay(BaseModel):
    type: str = "star_rating"
    position: str = "bottom_right"
    always_visible: bool = True


class ScreenMetadata(BaseModel):
    agent: str
    ab_test_id: Optional[str] = None
    variant: Optional[str] = None
    generated_at: Optional[str] = None

    class Config:
        extra = "allow"


class ScreenSpec(BaseModel):
    screen_id: str
    type: str
    layout: str
    components: List[ScreenComponent]
    feedback_overlay: FeedbackOverlay = Field(
        default_factory=lambda: FeedbackOverlay()
    )
    metadata: ScreenMetadata

    class Config:
        extra = "allow"


class ScreenRequest(BaseModel):
    context: Optional[str] = None  # hint to Ora about what kind of screen
    goal_id: Optional[str] = None
    domain: Optional[DomainType] = None  # optional domain filter
    feed_mode: Optional[Literal["now", "future", "path"]] = "path"
    exclude_future_events: Optional[bool] = None  # True = hard-filter scheduled/future events from legacy Now feed


class ScreenResponse(BaseModel):
    screen: ScreenSpec
    screen_spec_db_id: str  # DB id for the stored spec
    screens_today: int
    daily_limit: int
    is_limited: bool


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

class FeedbackSubmit(BaseModel):
    # Legacy card-feedback fields
    screen_spec_id: Optional[str] = None
    rating: Optional[int] = Field(None, ge=1, le=5)
    time_on_screen_ms: Optional[int] = None
    exit_point: Optional[str] = None
    completed: bool = False
    metadata: Optional[Dict[str, Any]] = None
    # Global in-app feedback fields
    category: Optional[Literal["Bug", "Malfunction", "Bad Card/Node", "Confusing", "Idea", "Design", "Praise", "Other"]] = None
    message: Optional[str] = None
    route: Optional[str] = None
    screenshot_data_url: Optional[str] = None


class FeedbackResponse(BaseModel):
    ok: bool
    fulfilment_delta: float = 0.0
    message: str = ""
    xp_earned: int = 0
    cp_earned: int = 0
    cp_balance: Optional[int] = None
    total_dao_cp: Optional[int] = None
    contribution_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

class GoalStep(BaseModel):
    id: str
    text: str
    detail: Optional[str] = None          # Ora's explanation of why/how
    resources: Optional[List[dict]] = []   # [{"label": str, "url": str}]
    completed: bool = False
    order: int = 0
    aura_note: Optional[str] = None         # Ora's note after user completes it


class GoalCreate(BaseModel):
    title: str
    description: Optional[str] = None
    steps: Optional[List[GoalStep]] = None
    domain: Optional[DomainType] = None
    intention_text: Optional[str] = None
    measurable_outcome: Optional[str] = None
    success_metric: Optional[str] = None
    target_value: Optional[str] = None
    target_date: Optional[str] = None
    graph_metadata: Optional[Dict[str, Any]] = None


class GoalUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None  # "active", "completed", "paused"
    steps: Optional[List[GoalStep]] = None
    progress: Optional[float] = Field(None, ge=0.0, le=1.0)
    domain: Optional[DomainType] = None
    intention_text: Optional[str] = None
    measurable_outcome: Optional[str] = None
    success_metric: Optional[str] = None
    target_value: Optional[str] = None
    target_date: Optional[str] = None
    graph_metadata: Optional[Dict[str, Any]] = None


class GoalOut(BaseModel):
    id: UUID
    title: str
    description: Optional[str]
    status: str
    steps: List[Dict[str, Any]]
    progress: float
    created_at: datetime
    domain: Optional[str] = None
    intention_text: Optional[str] = None
    measurable_outcome: Optional[str] = None
    success_metric: Optional[str] = None
    target_value: Optional[str] = None
    target_date: Optional[str] = None
    graph_metadata: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Monetization
# ---------------------------------------------------------------------------

class SubscriptionUpgrade(BaseModel):
    payment_method_token: str  # Stripe token or similar
    plan: str = "premium_monthly"


class AffiliateClick(BaseModel):
    tracking_id: str
    screen_spec_id: str
    url: str


class AffiliateConversion(BaseModel):
    tracking_id: str
    amount_cents: Optional[int] = None


class AdminInsights(BaseModel):
    total_users: int
    active_today: int
    premium_users: int
    avg_fulfilment_score: float
    total_revenue_cents: int
    top_agents: List[Dict[str, Any]]
    avg_rating_by_agent: Dict[str, float]
