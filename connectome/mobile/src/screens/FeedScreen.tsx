import React, { useEffect, useRef, useState, useCallback } from 'react';
import {
  View,
  Text,
  StyleSheet,
  Dimensions,
  ActivityIndicator,
  TouchableOpacity,
  PanResponder,
  Animated,
  Alert,
} from 'react-native';
import { getToken } from '../lib/auth';
import { getScreen, sendInteraction, ScreenCard } from '../lib/api';
import { getCurrentUserId } from '../lib/auth';

const { height: SCREEN_HEIGHT, width: SCREEN_WIDTH } = Dimensions.get('window');
const SWIPE_THRESHOLD = 80;

export default function FeedScreen() {
  const [card, setCard] = useState<ScreenCard | null>(null);
  const [loading, setLoading] = useState(true);
  const [userId, setUserId] = useState<string | null>(null);
  const [ratingVisible, setRatingVisible] = useState(false);
  const translateY = useRef(new Animated.Value(0)).current;
  const cardOpacity = useRef(new Animated.Value(1)).current;

  useEffect(() => {
    (async () => {
      const token = await getToken();
      if (token) {
        const uid = getCurrentUserId(token);
        setUserId(uid);
        await fetchCard(uid);
      }
    })();
  }, []);

  const fetchCard = useCallback(async (uid?: string | null) => {
    const id = uid ?? userId;
    if (!id) return;
    setLoading(true);
    try {
      const c = await getScreen(id);
      setCard(c);
      translateY.setValue(0);
      cardOpacity.setValue(1);
    } catch (err: any) {
      const msg = err?.response?.data?.detail ?? 'Failed to load content.';
      Alert.alert('Error', msg);
    } finally {
      setLoading(false);
    }
  }, [userId]);

  const animateOut = (callback: () => void) => {
    Animated.parallel([
      Animated.timing(translateY, {
        toValue: -SCREEN_HEIGHT * 0.6,
        duration: 280,
        useNativeDriver: true,
      }),
      Animated.timing(cardOpacity, {
        toValue: 0,
        duration: 280,
        useNativeDriver: true,
      }),
    ]).start(callback);
  };

  const panResponder = useRef(
    PanResponder.create({
      onMoveShouldSetPanResponder: (_, gs) =>
        Math.abs(gs.dy) > 10 && Math.abs(gs.dy) > Math.abs(gs.dx),
      onPanResponderMove: (_, gs) => {
        if (gs.dy < 0) translateY.setValue(gs.dy);
      },
      onPanResponderRelease: (_, gs) => {
        if (gs.dy < -SWIPE_THRESHOLD) {
          animateOut(() => fetchCard());
        } else {
          Animated.spring(translateY, {
            toValue: 0,
            useNativeDriver: true,
          }).start();
        }
      },
    }),
  ).current;

  const handleRate = async (stars: number) => {
    if (!card) return;
    setRatingVisible(false);
    try {
      await sendInteraction({ screen_id: card.screen_id, rating: stars, action: 'rate' });
    } catch {}
  };

  const handleSave = async () => {
    if (!card) return;
    try {
      await sendInteraction({ screen_id: card.screen_id, action: 'save' });
      Alert.alert('Saved ✓', 'Added to your collection.');
    } catch {}
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color="#8b5cf6" />
      </View>
    );
  }

  if (!card) {
    return (
      <View style={styles.center}>
        <Text style={styles.emptyText}>No content yet</Text>
        <TouchableOpacity style={styles.retryBtn} onPress={() => fetchCard()}>
          <Text style={styles.retryText}>Retry</Text>
        </TouchableOpacity>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Animated.View
        style={[
          styles.card,
          { transform: [{ translateY }], opacity: cardOpacity },
        ]}
        {...panResponder.panHandlers}
      >
        {/* Category badge */}
        <View style={styles.badge}>
          <Text style={styles.badgeText}>{card.category ?? 'insight'}</Text>
        </View>

        {/* Content */}
        <Text style={styles.title}>{card.title}</Text>
        <Text style={styles.description}>{card.description}</Text>

        {/* Swipe hint */}
        <View style={styles.swipeHint}>
          <Text style={styles.swipeHintText}>↑ swipe for next</Text>
        </View>
      </Animated.View>

      {/* Right side actions */}
      <View style={styles.actions}>
        <TouchableOpacity
          style={styles.actionBtn}
          onPress={() => setRatingVisible(!ratingVisible)}
        >
          <Text style={styles.actionIcon}>⭐</Text>
        </TouchableOpacity>
        <TouchableOpacity style={styles.actionBtn} onPress={handleSave}>
          <Text style={styles.actionIcon}>💾</Text>
        </TouchableOpacity>
      </View>

      {/* Star rating overlay */}
      {ratingVisible && (
        <View style={styles.ratingOverlay}>
          <View style={styles.ratingBox}>
            <Text style={styles.ratingTitle}>Rate this</Text>
            <View style={styles.stars}>
              {[1, 2, 3, 4, 5].map((n) => (
                <TouchableOpacity key={n} onPress={() => handleRate(n)}>
                  <Text style={styles.starBtn}>{'★'}</Text>
                </TouchableOpacity>
              ))}
            </View>
          </View>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#0a0a0a',
    justifyContent: 'center',
    alignItems: 'center',
  },
  center: {
    flex: 1,
    backgroundColor: '#0a0a0a',
    justifyContent: 'center',
    alignItems: 'center',
  },
  card: {
    width: SCREEN_WIDTH - 32,
    minHeight: SCREEN_HEIGHT * 0.55,
    backgroundColor: '#1a1a1a',
    borderRadius: 20,
    padding: 24,
    borderWidth: 1,
    borderColor: '#2a2a2a',
    justifyContent: 'space-between',
  },
  badge: {
    alignSelf: 'flex-start',
    backgroundColor: '#8b5cf620',
    borderRadius: 20,
    paddingHorizontal: 12,
    paddingVertical: 5,
    marginBottom: 20,
    borderWidth: 1,
    borderColor: '#8b5cf640',
  },
  badgeText: { color: '#8b5cf6', fontSize: 12, fontWeight: '600', textTransform: 'uppercase' },
  title: {
    fontSize: 26,
    fontWeight: '700',
    color: '#fff',
    lineHeight: 34,
    marginBottom: 16,
  },
  description: {
    fontSize: 16,
    color: '#aaa',
    lineHeight: 24,
    flex: 1,
  },
  swipeHint: { alignItems: 'center', marginTop: 20 },
  swipeHintText: { color: '#444', fontSize: 13 },
  actions: {
    position: 'absolute',
    right: 20,
    bottom: 100,
    gap: 16,
    flexDirection: 'column',
    alignItems: 'center',
  },
  actionBtn: {
    width: 52,
    height: 52,
    borderRadius: 26,
    backgroundColor: '#1a1a1a',
    borderWidth: 1,
    borderColor: '#2a2a2a',
    justifyContent: 'center',
    alignItems: 'center',
  },
  actionIcon: { fontSize: 22 },
  ratingOverlay: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 0,
    backgroundColor: '#000000aa',
    padding: 24,
    alignItems: 'center',
  },
  ratingBox: {
    backgroundColor: '#1a1a1a',
    borderRadius: 16,
    padding: 20,
    alignItems: 'center',
    width: '100%',
    borderWidth: 1,
    borderColor: '#2a2a2a',
  },
  ratingTitle: { color: '#fff', fontSize: 16, fontWeight: '600', marginBottom: 16 },
  stars: { flexDirection: 'row', gap: 12 },
  starBtn: { color: '#8b5cf6', fontSize: 32 },
  emptyText: { color: '#666', fontSize: 16, marginBottom: 16 },
  retryBtn: {
    backgroundColor: '#8b5cf6',
    borderRadius: 10,
    paddingHorizontal: 20,
    paddingVertical: 10,
  },
  retryText: { color: '#fff', fontWeight: '600' },
});
