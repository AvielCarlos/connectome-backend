import React, { useEffect, useState } from 'react';
import { View, ActivityIndicator, StyleSheet } from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { Ionicons } from '@expo/vector-icons';

import { getToken } from './src/lib/auth';
import LoginScreen from './src/screens/LoginScreen';
import RegisterScreen from './src/screens/RegisterScreen';
import FeedScreen from './src/screens/FeedScreen';
import OraScreen from './src/screens/OraScreen';
import ProfileScreen from './src/screens/ProfileScreen';

const AuthStack = createNativeStackNavigator();
const Tab = createBottomTabNavigator();

// ── Main tab navigator (shown when authenticated) ────────────────────────────

function MainTabs({ onSignOut }: { onSignOut: () => void }) {
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarStyle: {
          backgroundColor: '#0f0f0f',
          borderTopColor: '#1a1a1a',
          borderTopWidth: 1,
        },
        tabBarActiveTintColor: '#8b5cf6',
        tabBarInactiveTintColor: '#444',
        tabBarIcon: ({ color, size }) => {
          const icons: Record<string, React.ComponentProps<typeof Ionicons>['name']> = {
            Feed: 'home',
            Ora: 'chatbubble-ellipses',
            Profile: 'person',
          };
          return <Ionicons name={icons[route.name] ?? 'ellipse'} size={size} color={color} />;
        },
      })}
    >
      <Tab.Screen name="Feed" component={FeedScreen} />
      <Tab.Screen name="Ora" component={OraScreen} />
      <Tab.Screen
        name="Profile"
        children={() => <ProfileScreen onSignOut={onSignOut} />}
      />
    </Tab.Navigator>
  );
}

// ── Root component ────────────────────────────────────────────────────────────

export default function App() {
  const [authState, setAuthState] = useState<'checking' | 'authed' | 'unauthed'>('checking');

  useEffect(() => {
    checkAuth();
  }, []);

  const checkAuth = async () => {
    const token = await getToken();
    setAuthState(token ? 'authed' : 'unauthed');
  };

  if (authState === 'checking') {
    return (
      <View style={styles.splash}>
        <ActivityIndicator size="large" color="#8b5cf6" />
      </View>
    );
  }

  return (
    <NavigationContainer>
      {authState === 'authed' ? (
        <MainTabs onSignOut={() => setAuthState('unauthed')} />
      ) : (
        <AuthStack.Navigator screenOptions={{ headerShown: false }}>
          <AuthStack.Screen
            name="Login"
            children={({ navigation }) => (
              <LoginScreen
                navigation={navigation}
                onAuthSuccess={() => setAuthState('authed')}
              />
            )}
          />
          <AuthStack.Screen
            name="Register"
            children={({ navigation }) => (
              <RegisterScreen
                navigation={navigation}
                onAuthSuccess={() => setAuthState('authed')}
              />
            )}
          />
        </AuthStack.Navigator>
      )}
    </NavigationContainer>
  );
}

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    backgroundColor: '#0a0a0a',
    justifyContent: 'center',
    alignItems: 'center',
  },
});
